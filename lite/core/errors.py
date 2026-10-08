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

# Ordered: first match wins, so the specific bot-check and age-gate patterns
# must precede the generic "sign in" catch-all below them.
#
# Match against text that has been punctuation-normalised by _normalise(): the
# platform writes "you’re" with U+2019, and a pattern written with an ASCII
# apostrophe silently fails on it. That exact mismatch used to misreport every
# datacenter bot-check as "private or age-restricted", which points the
# operator at the wrong fix entirely.
_PATTERNS: tuple[tuple[str, int, str, str], ...] = (
    (
        r"sign in to confirm (that )?(you'?re |you are )?not a bot|"
        r"confirm (you'?re|you are) not a bot|suspected as a bot|"
        r"bot.?detect|captcha|unusual traffic|automated queries",
        403,
        "blocked_by_platform",
        "The platform asked this server to prove it is not a bot. Datacenter IPs "
        "(Render, Cloud Run, EC2) are challenged far more often than home IPs. "
        "Try IMPERSONATE=chrome first, then COOKIES_B64, then a residential PROXY.",
    ),
    (
        r"sign in to confirm your age|confirm your age|age.?restricted|age.?gated|"
        r"inappropriate for some users|may be inappropriate",
        403,
        "age_restricted",
        "Age-gated media requires a signed-in session. Set COOKIES_B64 to the base64 "
        "of a Netscape cookies.txt exported from an account that can view it.",
    ),
    (
        r"login required|requested format is not available.*login|this video is private|"
        r"private video|members[- ]only|join this channel|sign in|account.*required|"
        r"requires authentication|restricted video",
        403,
        "auth_required",
        "Either the media really is private, or the platform is withholding it from "
        "this server's IP. If the link plays in your own browser while logged out, "
        "treat it as a block: try IMPERSONATE=chrome, then COOKIES_B64.",
    ),
    # Tooling failures come before the content categories below. "ffmpeg not
    # found" would otherwise be caught by the media_unavailable pattern and
    # reported as a missing video, hiding a server misconfiguration.
    (
        r"ffmpeg|ffprobe",
        500,
        "ffmpeg_missing",
        "This conversion needs ffmpeg in the container. The provided Dockerfile "
        "installs it; a bare `pip install` host will not have it.",
    ),
    # Also ahead of media_unavailable: a takedown usually reads "no longer
    # available due to a copyright claim", and the reason is the useful part.
    (
        r"copyright|dmca|blocked it on copyright grounds",
        451,
        "legally_blocked",
        None,
    ),
    (
        r"available in your country|geo.?restricted|geo.?block|"
        r"blocked it in your country|not available from your location|"
        r"available in your location|not available in your region",
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
        r"requested format is not available|no video formats found|"
        r"no formats found",
        422,
        "no_suitable_format",
        "Relax the request: lower 'quality', or try mode=audio.",
    ),
    (
        r"unsupported url|no video could be found|unable to extract|"
        r"no suitable extractor|is not a valid url",
        400,
        "unsupported_url",
        "yt-dlp has no extractor for this link, or the page holds no media.",
    ),
    (
        r"timed out|timeout|connection reset|connection aborted|"
        r"temporary failure in name resolution|network is unreachable",
        504,
        "upstream_timeout",
        "The media host was slow or unreachable. Retry.",
    ),
    # Last of the specific categories, and deliberately so: its phrases are
    # broad enough to swallow the ones above if it ran first. "not found" is
    # qualified for the same reason.
    (
        r"video unavailable|removed by the uploader|no longer available|"
        r"has been terminated|does not exist|video not found|media not found|"
        r"http error 404|has been deleted|was deleted|account.*not exist",
        404,
        "media_unavailable",
        None,
    ),
)


_SMART_PUNCTUATION = {
    "\u2018": "'",  # left single quote
    "\u2019": "'",  # right single quote - YouTube's "you’re not a bot"
    "\u02bc": "'",  # modifier letter apostrophe
    "\u201c": '"',
    "\u201d": '"',
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
    "\u00a0": " ",  # non-breaking space
}


def _normalise(message: str) -> str:
    """Strip ANSI codes, the ERROR: prefix, and typographic punctuation.

    Platforms write user-facing prose with smart quotes and dashes. Matching
    them with ASCII patterns fails in ways that are invisible on screen, so
    normalise before any regex touches the text.
    """
    cleaned = re.sub(r"\x1b\[[0-9;]*m", "", message)
    cleaned = re.sub(r"^ERROR:\s*", "", cleaned).strip()
    for fancy, plain in _SMART_PUNCTUATION.items():
        cleaned = cleaned.replace(fancy, plain)
    return cleaned


def from_ytdlp(exc: BaseException) -> ApiError:
    """Translate a yt-dlp exception into an ApiError with an actionable hint."""
    raw = str(exc) or exc.__class__.__name__
    cleaned = _normalise(raw)
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
