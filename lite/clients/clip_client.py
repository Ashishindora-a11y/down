"""Python client for the Clip Download API (Lite).

Drop this file into the application that consumes the API (for example your
Google Cloud app) and talk to the service through it.

    pip install requests

    from clip_client import ClipClient

    client = ClipClient("https://clip-api-xxxx.run.app", api_key="...")
    meta = client.info("https://youtube.com/shorts/VIDEO_ID")
    print(meta["title"], meta["duration_sec"])

    # Recommended: resolve the CDN URL, then pull the bytes from the CDN.
    # Nothing transits the API service, so you pay no egress there and cannot
    # hit its request timeout.
    client.download_direct("https://youtube.com/shorts/VIDEO_ID", "clip.mp4")

    # Fallback: relay through the API. Needed when the CDN demands headers your
    # environment cannot send, or when you want server-side mp3 conversion.
    client.download_proxied("https://youtube.com/shorts/VIDEO_ID", "clip.mp4")

Two operational notes:

  * CDN URLs expire (minutes to hours) and are frequently bound to the IP that
    resolved them. ``download_direct`` detects a refusal and retries through
    the proxy path automatically.
  * If the service runs as a private Cloud Run service, Cloud Run consumes the
    ``Authorization`` header for IAM. Pass ``id_token=`` for that and keep the
    application key in ``api_key=``; they travel in different headers and do
    not collide.
"""

from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import requests

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = (10, 300)  # (connect, read) seconds
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ClipApiError(RuntimeError):
    """An error returned by the API, carrying its machine-readable code."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        request_id: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        detail = f"[{status} {code}] {message}"
        if hint:
            detail += f" | hint: {hint}"
        if request_id:
            detail += f" | request_id: {request_id}"
        super().__init__(detail)
        self.status = status
        self.code = code
        self.message = message
        self.hint = hint
        self.request_id = request_id
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE_STATUS

    @property
    def needs_credentials(self) -> bool:
        """True when the platform wants cookies or a different egress IP."""
        return self.code in {"auth_required", "blocked_by_platform", "geo_blocked"}


class ClipClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        id_token: str | None = None,
        timeout: tuple[int, int] | int = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        session: requests.Session | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.id_token = id_token
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self._session = session or requests.Session()

    # --- plumbing ---------------------------------------------------------

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if self.id_token:
            # Cloud Run IAM reads this; it never reaches the application.
            headers["Authorization"] = f"Bearer {self.id_token}"
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _raise(response: requests.Response) -> None:
        payload: dict[str, Any] = {}
        try:
            payload = response.json()
        except ValueError:
            pass
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            raise ClipApiError(
                response.status_code,
                error.get("code", "unknown"),
                error.get("message", response.reason or "request failed"),
                hint=error.get("hint"),
                request_id=error.get("request_id") or response.headers.get("X-Request-Id"),
                retry_after=error.get("retry_after"),
            )
        raise ClipApiError(
            response.status_code,
            "http_error",
            f"{response.reason or 'request failed'}: {response.text[:200]}",
            request_id=response.headers.get("X-Request-Id"),
        )

    def _with_retries(self, attempt: Callable[[], requests.Response]) -> requests.Response:
        """Retry idempotent calls with exponential backoff and jitter.

        Honours Retry-After when the server sends it, which it does for 429
        (rate limit) and 503 (all transfer slots busy).
        """
        last: ClipApiError | None = None
        for index in range(self.max_retries + 1):
            response = attempt()
            if response.ok:
                return response

            try:
                self._raise(response)
            except ClipApiError as exc:
                last = exc
                if not exc.retryable or index == self.max_retries:
                    raise
                delay = exc.retry_after or min(2**index, 30)
                delay += random.uniform(0, 0.5)
                log.warning(
                    "clip api %s; retrying in %.1fs (attempt %d/%d)",
                    exc.code,
                    delay,
                    index + 1,
                    self.max_retries,
                )
                response.close()
                time.sleep(delay)
        assert last is not None
        raise last

    # --- metadata ---------------------------------------------------------

    def limits(self) -> dict:
        """Service caps. Cheap, unauthenticated, worth caching at startup."""
        response = self._session.get(
            f"{self.base_url}/api/v1/limits", headers=self._headers(), timeout=self.timeout
        )
        if not response.ok:
            self._raise(response)
        return response.json()

    def info(self, url: str, *, formats: bool = True) -> dict:
        """Metadata and available formats. Downloads nothing."""
        response = self._with_retries(
            lambda: self._session.post(
                f"{self.base_url}/api/v1/info",
                json={"url": url, "formats": formats},
                headers=self._headers(),
                timeout=self.timeout,
            )
        )
        return response.json()

    def direct_url(self, url: str, *, mode: str = "video", quality: str | int = "best") -> dict:
        """Resolve the CDN URL. No bytes move through the API service."""
        response = self._with_retries(
            lambda: self._session.post(
                f"{self.base_url}/api/v1/url",
                json={"url": url, "mode": mode, "quality": str(quality)},
                headers=self._headers(),
                timeout=self.timeout,
            )
        )
        return response.json()

    # --- downloads --------------------------------------------------------

    def stream_proxied(
        self,
        url: str,
        *,
        mode: str = "video",
        quality: str | int = "best",
        via: str = "auto",
        audio_format: str = "original",
        container: str = "auto",
        chunk_size: int = 1 << 18,
    ) -> Iterator[bytes]:
        """Yield media bytes relayed through the API service."""
        params = {
            "url": url,
            "mode": mode,
            "quality": str(quality),
            "via": via,
            "audio_format": audio_format,
            "container": container,
        }
        response = self._session.get(
            f"{self.base_url}/api/v1/download",
            params=params,
            headers=self._headers({"Accept": "*/*"}),
            timeout=self.timeout,
            stream=True,
        )
        if not response.ok:
            try:
                self._raise(response)
            finally:
                response.close()
        try:
            yield from response.iter_content(chunk_size=chunk_size)
        finally:
            response.close()

    def download_proxied(self, url: str, destination: str | Path, **kwargs: Any) -> Path:
        """Relay through the API service and write to ``destination``."""
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a sibling temp file so a failure never leaves a truncated
        # file that looks complete.
        staging = target.with_suffix(target.suffix + ".part")
        written = 0
        try:
            with staging.open("wb") as handle:
                for chunk in self.stream_proxied(url, **kwargs):
                    handle.write(chunk)
                    written += len(chunk)
            if written == 0:
                raise ClipApiError(502, "empty_response", "The API returned no bytes.")
            staging.replace(target)
        finally:
            staging.unlink(missing_ok=True)
        log.info("downloaded %s (%.2f MB) via proxy", target.name, written / 1048576)
        return target

    def download_direct(
        self,
        url: str,
        destination: str | Path,
        *,
        mode: str = "video",
        quality: str | int = "best",
        fallback_to_proxy: bool = True,
        chunk_size: int = 1 << 18,
    ) -> Path:
        """Resolve the CDN URL, then fetch the bytes straight from the CDN.

        The cheapest path: the API service spends no bandwidth and no request
        time. Falls back to the proxy path when the CDN rejects this host,
        which happens when the media URL is bound to the resolving IP.
        """
        resolved = self.direct_url(url, mode=mode, quality=quality)

        if resolved.get("delivery") == "split_streams":
            if not fallback_to_proxy:
                raise ClipApiError(
                    422,
                    "split_streams",
                    "Video and audio are separate at this quality and must be muxed.",
                    hint="Allow fallback_to_proxy, or mux the two URLs with ffmpeg yourself.",
                )
            log.info("split streams at this quality; using the server-side merge path")
            return self.download_proxied(
                url, destination, mode=mode, quality=quality, via="server"
            )

        descriptor = resolved.get("file") or {}
        media_url = descriptor.get("url")
        if not media_url:
            raise ClipApiError(502, "no_url", "The API returned no media URL.")

        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_suffix(target.suffix + ".part")

        try:
            # Replay the headers the API supplied; many CDNs check Referer.
            response = self._session.get(
                media_url,
                headers=descriptor.get("http_headers") or {},
                timeout=self.timeout,
                stream=True,
            )
            if response.status_code in (401, 403, 404):
                response.close()
                if not fallback_to_proxy:
                    raise ClipApiError(
                        response.status_code,
                        "cdn_refused",
                        "The CDN refused this host. The URL is likely IP-locked or expired.",
                    )
                log.info(
                    "CDN returned %s; falling back to the proxy path", response.status_code
                )
                return self.download_proxied(url, destination, mode=mode, quality=quality)
            response.raise_for_status()

            written = 0
            with staging.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    handle.write(chunk)
                    written += len(chunk)
            response.close()

            if written == 0:
                raise ClipApiError(502, "empty_response", "The CDN returned no bytes.")
            staging.replace(target)
            log.info("downloaded %s (%.2f MB) direct from CDN", target.name, written / 1048576)
            return target
        finally:
            staging.unlink(missing_ok=True)

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "ClipClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


# --- Google Cloud helper --------------------------------------------------


def for_private_cloud_run(service_url: str, api_key: str, **kwargs: Any) -> ClipClient:
    """Build a client for a Cloud Run service deployed without public access.

    Requires `google-auth`. The calling service account needs roles/run.invoker
    on the target service.
    """
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2 import id_token as google_id_token

    token = google_id_token.fetch_id_token(GoogleRequest(), service_url)
    return ClipClient(service_url, api_key=api_key, id_token=token, **kwargs)


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Clip Download API client")
    parser.add_argument("url", help="Media URL to resolve or download")
    parser.add_argument("--base", required=True, help="API base URL")
    parser.add_argument("--key", help="API key")
    parser.add_argument("-o", "--output", help="Write the media to this path")
    parser.add_argument("--mode", default="video", choices=["video", "audio"])
    parser.add_argument("--quality", default="best")
    parser.add_argument(
        "--proxy-path",
        action="store_true",
        help="Relay bytes through the API instead of fetching from the CDN",
    )
    args = parser.parse_args()

    with ClipClient(args.base, api_key=args.key) as client:
        if not args.output:
            import json

            meta = client.info(args.url)
            print(json.dumps(meta, indent=2)[:4000])
        elif args.proxy_path:
            client.download_proxied(
                args.url, args.output, mode=args.mode, quality=args.quality
            )
        else:
            client.download_direct(
                args.url, args.output, mode=args.mode, quality=args.quality
            )
