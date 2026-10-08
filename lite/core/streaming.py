"""Proxy-streaming of media bytes.

The default download path never touches disk: we resolve the media URL with
yt-dlp, open it server-side with the headers the platform requires, and relay
the bytes straight to the caller in fixed-size chunks.

Two reasons this matters on serverless hosts:
  * Cloud Run's filesystem is backed by RAM, so writing a file to /tmp spends
    the memory budget the container is billed for.
  * Bytes start flowing immediately, so the client is not waiting on a full
    download before the first byte (relevant under App Runner's 120s cap).

Memory stays flat at roughly one chunk regardless of clip size.
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
from typing import Callable, Iterator

from .errors import ApiError, too_large

log = logging.getLogger("clipapi.streaming")

# Headers we never relay back to the caller.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "set-cookie",
    "set-cookie2",
}


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect hop.

    Without this, an open redirect on a media host could still walk the request
    to 169.254.169.254 or an internal service after the initial URL passed its
    SSRF check.
    """

    max_redirections = 5

    def __init__(self, validator: Callable[[str], str]) -> None:
        super().__init__()
        self._validator = validator

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        try:
            self._validator(newurl)
        except ApiError as exc:
            raise urllib.error.HTTPError(
                newurl, code, f"blocked redirect target: {exc.message}", headers, fp
            ) from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener(validator: Callable[[str], str], proxy: str) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = [_GuardedRedirectHandler(validator)]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        # Do not silently inherit ambient *_proxy env vars.
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


class Upstream:
    """An opened upstream media response."""

    def __init__(self, response, status: int, headers: dict[str, str]) -> None:
        self._response = response
        self.status = status
        self.headers = headers

    @property
    def content_length(self) -> int | None:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    @property
    def content_type(self) -> str | None:
        return self.headers.get("Content-Type")

    def close(self) -> None:
        try:
            self._response.close()
        except Exception:  # pragma: no cover
            pass

    def iter_chunks(
        self,
        *,
        chunk_size: int,
        deadline: float,
        max_bytes: int,
    ) -> Iterator[bytes]:
        """Yield the body. Stops at the deadline or the byte cap."""
        sent = 0
        try:
            while True:
                if time.monotonic() > deadline:
                    log.warning(
                        "stream aborted at deadline after %d bytes; client sees a short read",
                        sent,
                    )
                    return
                chunk = self._response.read(chunk_size)
                if not chunk:
                    return
                sent += len(chunk)
                if sent > max_bytes:
                    log.warning("stream aborted at byte cap after %d bytes", sent)
                    return
                yield chunk
        except (urllib.error.URLError, OSError) as exc:
            log.warning("upstream read failed after %d bytes: %s", sent, exc)
            return
        finally:
            self.close()


def open_upstream(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    range_header: str | None = None,
    timeout: int = 20,
    proxy: str = "",
    validator: Callable[[str], str] | None = None,
    max_bytes: int | None = None,
) -> Upstream:
    """Open a media URL for relaying. Raises ApiError on upstream failure."""
    request_headers = dict(headers or {})
    request_headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )
    # Identity encoding keeps Content-Length meaningful and avoids double work.
    request_headers["Accept-Encoding"] = "identity"
    if range_header:
        request_headers["Range"] = range_header

    opener = _build_opener(validator or (lambda u: u), proxy)
    request = urllib.request.Request(url, headers=request_headers, method="GET")

    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body_hint = ""
        try:
            body_hint = exc.read(200).decode("utf-8", "replace")
        except Exception:  # pragma: no cover
            pass
        finally:
            exc.close()
        raise _map_http_error(exc.code, body_hint) from exc
    except urllib.error.URLError as exc:
        raise ApiError(
            504,
            "upstream_timeout",
            f"Could not reach the media host: {exc.reason}",
        ) from exc
    except OSError as exc:
        raise ApiError(502, "upstream_error", f"Media host connection failed: {exc}") from exc

    status = getattr(response, "status", None) or response.getcode() or 200
    raw_headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in _HOP_BY_HOP
    }
    upstream = Upstream(response, status, raw_headers)

    declared = upstream.content_length
    if max_bytes is not None and declared is not None and declared > max_bytes:
        upstream.close()
        raise too_large(
            f"Media is {declared / 1048576:.1f} MB, over the {max_bytes / 1048576:.0f} MB cap.",
            hint="Request a lower 'quality', or raise MAX_FILESIZE_MB.",
        )
    return upstream


def _map_http_error(code: int, body_hint: str) -> ApiError:
    detail = f" Upstream said: {body_hint.strip()[:160]}" if body_hint.strip() else ""
    if code in (401, 403):
        return ApiError(
            403,
            "upstream_forbidden",
            f"The media host refused this server's request (HTTP {code}).{detail}",
            hint=(
                "Media URLs are usually IP- and time-locked. Resolve and download from the "
                "same place, or use POST /api/v1/url and fetch from the client that will "
                "consume it."
            ),
        )
    if code == 404:
        return ApiError(404, "media_unavailable", "The media URL has expired or moved.")
    if code == 429:
        return ApiError(
            429,
            "upstream_rate_limited",
            "The media host is throttling this server's IP.",
            retry_after=30,
        )
    if code == 416:
        return ApiError(416, "range_not_satisfiable", "The requested byte range is invalid.")
    return ApiError(502, "upstream_error", f"Media host returned HTTP {code}.{detail}")
