"""Environment-driven settings.

Every knob is an env var so the same image can be deployed to App Runner,
Cloud Run, ECS, Lightsail or a plain Docker host without a rebuild.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("clipapi.config")

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    log.warning("env %s=%r is not a boolean, using default %s", name, raw, default)
    return default


def _int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError:
            log.warning("env %s=%r is not an integer, using default %s", name, raw, default)
            value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    # --- server -----------------------------------------------------------
    port: int
    log_level: str
    enable_ui: bool
    public_base_url: str
    trust_proxy_headers: bool
    # "full"     - can relay bytes and run ffmpeg. Needs a real container.
    # "resolver" - metadata and CDN redirects only, no bytes through the
    #              service. The mode for hosts that cap response bodies or
    #              forbid native binaries, Vercel above all.
    service_mode: str

    # --- auth / access ----------------------------------------------------
    api_keys: frozenset[str]
    require_api_key: bool
    cors_origins: tuple[str, ...]

    # --- clip limits (this is what makes it "lite") -----------------------
    max_duration_sec: int
    max_filesize_mb: int
    # Cap on the SHORT side of the frame, not on height. Reels, Shorts and
    # TikToks are portrait: a "1080p" vertical clip is 1080x1920, so a cap on
    # height would reject the full-quality version of the exact media this
    # service exists to fetch.
    max_quality: int
    allow_live: bool

    # --- runtime guards ---------------------------------------------------
    soft_deadline_sec: int
    max_concurrent: int
    rate_per_min: int
    rate_per_day: int
    socket_timeout: int
    chunk_size: int

    # --- feature switches -------------------------------------------------
    enable_server_download: bool
    allow_generic_extractor: bool
    ssrf_guard: bool
    allowed_domains: tuple[str, ...]
    blocked_domains: tuple[str, ...]

    # --- yt-dlp plumbing --------------------------------------------------
    proxy: str
    user_agent: str
    cookies_file: str
    extractor_args: str
    tmp_dir: Path

    # --- caching ----------------------------------------------------------
    cache_ttl_sec: int
    cache_max_entries: int

    # --- derived ----------------------------------------------------------
    warnings: tuple[str, ...] = field(default=())

    @property
    def max_filesize_bytes(self) -> int:
        return self.max_filesize_mb * 1024 * 1024

    @property
    def auth_enabled(self) -> bool:
        return self.require_api_key and bool(self.api_keys)

    @property
    def resolver_only(self) -> bool:
        return self.service_mode == "resolver"

    def public_limits(self) -> dict:
        """Caps advertised to clients via GET /api/v1/limits."""
        return {
            "max_duration_sec": self.max_duration_sec,
            "max_filesize_mb": self.max_filesize_mb,
            "max_quality": self.max_quality,
            "quality_measured_on": "short side of the frame (1080 = 1080p, "
            "landscape or portrait)",
            "allow_live": self.allow_live,
            "max_concurrent": self.max_concurrent,
            "rate_limit_per_min": self.rate_per_min,
            "rate_limit_per_day": self.rate_per_day,
            "soft_deadline_sec": self.soft_deadline_sec,
            "server_download_enabled": self.enable_server_download and not self.resolver_only,
            "auth_required": self.auth_enabled,
            "service_mode": self.service_mode,
        }


def _resolve_cookies(tmp_dir: Path) -> tuple[str, list[str]]:
    """Return a cookies.txt path, materialising COOKIES_B64 if provided."""
    notes: list[str] = []

    explicit = _str("COOKIES_FILE")
    if explicit:
        if Path(explicit).is_file():
            return explicit, notes
        notes.append(f"COOKIES_FILE={explicit!r} does not exist; continuing without cookies.")
        return "", notes

    encoded = _str("COOKIES_B64")
    if not encoded:
        return "", notes

    try:
        blob = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        notes.append(f"COOKIES_B64 is not valid base64 ({exc}); continuing without cookies.")
        return "", notes

    target = tmp_dir / "cookies.txt"
    try:
        target.write_bytes(blob)
        os.chmod(target, 0o600)
    except OSError as exc:
        notes.append(f"could not write cookies to {target} ({exc}); continuing without cookies.")
        return "", notes

    notes.append(f"loaded cookies from COOKIES_B64 ({len(blob)} bytes)")
    return str(target), notes


def _quality_cap() -> int:
    """Resolve MAX_QUALITY, accepting the older MAX_HEIGHT name as an alias."""
    if os.environ.get("MAX_QUALITY", "").strip():
        return _int("MAX_QUALITY", 1080, minimum=144)
    if os.environ.get("MAX_HEIGHT", "").strip():
        log.info("MAX_HEIGHT is a deprecated alias for MAX_QUALITY; honouring it")
        return _int("MAX_HEIGHT", 1080, minimum=144)
    return 1080


def load() -> Settings:
    warnings: list[str] = []

    tmp_dir = Path(_str("TMP_DIR") or tempfile.gettempdir()) / "clipapi"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - fall back to system temp
        warnings.append(f"TMP_DIR {tmp_dir} unusable ({exc}); using system temp")
        tmp_dir = Path(tempfile.gettempdir())

    api_keys = frozenset(_csv("API_KEYS"))
    require_api_key = _bool("REQUIRE_API_KEY", True)
    if require_api_key and not api_keys:
        warnings.append(
            "SECURITY: REQUIRE_API_KEY is on but API_KEYS is empty, so every request is "
            "rejected. Set API_KEYS to one or more secrets, or set REQUIRE_API_KEY=false "
            "to run this service fully open."
        )
    if not require_api_key:
        warnings.append(
            "SECURITY: REQUIRE_API_KEY=false - this endpoint is UNAUTHENTICATED. Anyone who "
            "finds the URL can use your bandwidth and your IP reputation. Only do this behind "
            "a private VPC, an API gateway, or an IAM-authenticated Cloud Run service."
        )

    cors_origins = _csv("CORS_ORIGINS")
    if "*" in cors_origins and require_api_key:
        warnings.append(
            "CORS_ORIGINS='*' means any website can call this API from a browser. The API key "
            "would then be exposed in client-side code. Prefer calling from your backend, or "
            "list explicit origins."
        )

    cookies_file, cookie_notes = _resolve_cookies(tmp_dir)
    warnings.extend(cookie_notes)

    service_mode = _str("SERVICE_MODE", "full").lower()
    if service_mode not in {"full", "resolver"}:
        warnings.append(
            f"SERVICE_MODE={service_mode!r} is not recognised; falling back to 'full'. "
            "Valid values are 'full' and 'resolver'."
        )
        service_mode = "full"
    if service_mode == "resolver":
        warnings.append(
            "SERVICE_MODE=resolver: /api/v1/download is disabled. Clients get metadata "
            "and CDN redirects, and fetch media themselves. Note that YouTube video "
            "cannot be served this way, because it has no combined video+audio format "
            "and muxing needs ffmpeg."
        )

    settings = Settings(
        port=_int("PORT", 8080, minimum=1, maximum=65535),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        enable_ui=_bool("ENABLE_UI", True),
        public_base_url=_str("PUBLIC_BASE_URL").rstrip("/"),
        trust_proxy_headers=_bool("TRUST_PROXY_HEADERS", True),
        service_mode=service_mode,
        api_keys=api_keys,
        require_api_key=require_api_key,
        cors_origins=cors_origins,
        # 300s default gives headroom over the "2-3 minute" target.
        max_duration_sec=_int("MAX_DURATION_SEC", 300, minimum=1, maximum=24 * 3600),
        max_filesize_mb=_int("MAX_FILESIZE_MB", 250, minimum=1),
        max_quality=_quality_cap(),
        allow_live=_bool("ALLOW_LIVE", False),
        # App Runner kills any request at 120s wall clock, so stay under it.
        soft_deadline_sec=_int("SOFT_DEADLINE_SEC", 110, minimum=5, maximum=3600),
        max_concurrent=_int("MAX_CONCURRENT", 4, minimum=1, maximum=256),
        rate_per_min=_int("RATE_LIMIT_PER_MIN", 20, minimum=0),
        rate_per_day=_int("RATE_LIMIT_PER_DAY", 1000, minimum=0),
        socket_timeout=_int("SOCKET_TIMEOUT", 20, minimum=1),
        chunk_size=_int("CHUNK_SIZE_KB", 256, minimum=16, maximum=8192) * 1024,
        enable_server_download=_bool("ENABLE_SERVER_DOWNLOAD", True),
        allow_generic_extractor=_bool("ALLOW_GENERIC_EXTRACTOR", False),
        ssrf_guard=_bool("SSRF_GUARD", True),
        allowed_domains=_csv("ALLOWED_DOMAINS"),
        blocked_domains=_csv("BLOCKED_DOMAINS"),
        proxy=_str("PROXY"),
        user_agent=_str("USER_AGENT"),
        cookies_file=cookies_file,
        extractor_args=_str("EXTRACTOR_ARGS"),
        tmp_dir=tmp_dir,
        cache_ttl_sec=_int("CACHE_TTL_SEC", 300, minimum=0),
        cache_max_entries=_int("CACHE_MAX_ENTRIES", 512, minimum=1),
        warnings=tuple(warnings),
    )
    return settings
