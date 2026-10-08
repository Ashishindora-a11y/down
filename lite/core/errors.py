"""Typed API errors plus translation of yt-dlp failures into HTTP responses."""

from __future__ import annotations

import re


class ApiError(Exception):
    """An error that maps cleanly onto an HTTP response."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.hint = hint
        self.retry_after = retry_after

    def to_dict(self) -> dict:
        payload: dict = {"error": {"code": self.code, "message": self.message}}
        if self.hint:
            payload["error"]["hint"] = self.hint
        if self.retry_after is not None:
            payload["error"]["retry_after"] = self.retry_after
        return payload


# --- convenience constructors --------------------------------------------


def bad_request(message: str, *, hint: str | None = None) -> ApiError:
    return ApiError(400, "bad_request", message, hint=hint)


def unauthorized(message: str = "Missing or invalid API key.") -> ApiError:
    return ApiError(
        401,
        "unauthorized",
        message,
        hint="Send the key as 'X-API-Key: <key>', 'Authorization: Bearer <key>', or '?key=<key>'.",
    )


def too_large(message: str, *, hint: str | None = None) -> ApiError:
    return ApiError(413, "limit_exceeded", message, hint=hint)


def busy(retry_after: int = 5) -> ApiError:
    return ApiError(
        503,
        "server_busy",
        "All download slots are in use.",
        hint="Retry shortly, raise MAX_CONCURRENT, or scale out to more instances.",
        retry_after=retry_after,
    )


def rate_limited(scope: str, retry_after: int) -> ApiError:
    return ApiError(
        429,
        "rate_limited",
        f"Rate limit exceeded ({scope}).",
        retry_after=retry_after,
    )


def deadline_exceeded(seconds: int) -> ApiError:
    return ApiError(
        504,
        "deadline_exceeded",
        f"Download did not finish within {seconds}s and was aborted.",
        hint=(
            "Serverless hosts cap request duration (App Runner is a hard 120s). Use "
            "POST /api/v1/url to hand the direct media URL to your client instead of "
            "proxying the bytes through this service."
        ),
    )


# --- yt-dlp error translation --------------------------------------------

# Ordered: first match wins.
_PATTERNS: tuple[tuple[str, int, str, str], ...] = (
    (
        r"sign in to confirm (you'?re|your) (not a bot|age)|confirm your age|"
        r"bot.?detect|captcha",
        403,
        "blocked_by_platform",
        "The platform asked this server to prove it is not a bot. Datacenter IPs "
        "(App Runner, Cloud Run, EC2) are challenged far more often than home IPs.",
    ),
    (
        r"login required|requested format is not available.*login|this video is private|"
        r"private video|members[- ]only|join this channel|sign in|account.*required|"
        r"requires authentication|restricted video",
        403,
        "auth_required",
        "Supply cookies for an account that can view this media via the COOKIES_B64 "
        "env var (base64 of a Netscape cookies.txt).",
    ),
    (
        r"not available in your country|geo restricted|geo-restricted|"
        r"blocked it in your country|not available from your location",
        451,
        "geo_blocked",
        "Route the request through a proxy in a permitted region using the PROXY env var.",
    ),
    (
        r"http error 429|too many requests|rate.?limit",
        429,
        "upstream_rate_limited",
        "The platform is throttling this server's IP. Back off, rotate egress IPs, or "
        "set PROXY.",
    ),
    (
        r"video unavailable|removed by the uploader|no longer available|"
        r"has been terminated|does not exist|not found|http error 404|deleted",
        404,
        "media_unavailable",
        None,
    ),
    (
        r"unsupported url|no video could be found|unable to extract|"
        r"no suitable extractor|is not a valid url",
        400,
        "unsupported_url",
        "yt-dlp has no extractor for this link, or the page holds no media.",
    ),
    (
        r"requested format is not available|no video formats found|"
        r"no formats found",
        422,
        "no_suitable_format",
        "Relax the request: lower 'quality', or try mode=audio.",
    ),
    (
        r"timed out|timeout|connection reset|connection aborted|"
        r"temporary failure in name resolution|network is unreachable",
        504,
        "upstream_timeout",
        "The media host was slow or unreachable. Retry.",
    ),
    (
        r"ffmpeg|ffprobe",
        500,
        "ffmpeg_missing",
        "This conversion needs ffmpeg in the container. The provided Dockerfile "
        "installs it; a bare `pip install` host will not have it.",
    ),
    (
        r"copyright|dmca",
        451,
        "legally_blocked",
        None,
    ),
)


def from_ytdlp(exc: BaseException) -> ApiError:
    """Translate a yt-dlp exception into an ApiError with an actionable hint."""
    raw = str(exc) or exc.__class__.__name__
    # yt-dlp prefixes messages with "ERROR: " and ANSI colour codes.
    cleaned = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    cleaned = re.sub(r"^ERROR:\s*", "", cleaned).strip()
    lowered = cleaned.lower()

    for pattern, status, code, hint in _PATTERNS:
        if re.search(pattern, lowered):
            return ApiError(status, code, _trim(cleaned), hint=hint)

    return ApiError(
        502,
        "extraction_failed",
        _trim(cleaned),
        hint="If this link works in the yt-dlp CLI, rebuild the image to pick up a newer yt-dlp.",
    )


def _trim(message: str, limit: int = 400) -> str:
    one_line = " ".join(message.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1].rstrip() + "\u2026"
