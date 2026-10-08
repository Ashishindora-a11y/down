"""API key auth, CORS, rate limiting and concurrency control.

Limits are per process and held in memory. That is deliberate for a Lite
service: no Redis, no state, no extra moving parts. The consequence is that
with N instances the effective ceiling is N x the configured limit, so treat
these as guardrails against accidental hammering rather than as billing-grade
quota. Put API Gateway, Cloud Armor or a WAF in front if you need hard limits.
"""

from __future__ import annotations

import hmac
import logging
import threading
import time
from collections import defaultdict, deque

from .config import Settings
from .errors import ApiError, rate_limited, unauthorized

log = logging.getLogger("clipapi.security")


# --- authentication -------------------------------------------------------


def extract_key(headers, args) -> str:
    """Read the API key from the header or the query string.

    Query-string keys exist so a browser or an <a download> link can hit the
    endpoint directly. They do leak into access logs, so prefer the header for
    server-to-server calls.
    """
    header_key = headers.get("X-API-Key", "").strip()
    if header_key:
        return header_key

    authorization = headers.get("Authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()

    return (args.get("key") or "").strip()


def authenticate(cfg: Settings, headers, args) -> str | None:
    """Return the caller's key, or None when auth is disabled.

    Comparison is constant-time to avoid leaking the key through timing.
    """
    if not cfg.require_api_key:
        return None

    presented = extract_key(headers, args)
    if not presented:
        raise unauthorized("No API key supplied.")

    for known in cfg.api_keys:
        if hmac.compare_digest(presented, known):
            return presented

    raise unauthorized("The supplied API key is not recognised.")


def caller_id(cfg: Settings, api_key: str | None, access_route, remote_addr) -> str:
    """Stable identity for rate limiting: the API key if present, else the IP."""
    if api_key:
        return f"key:{api_key[:8]}:{len(api_key)}"

    if cfg.trust_proxy_headers and access_route:
        # Leftmost X-Forwarded-For entry. Spoofable by a direct caller, so the
        # API key is the real control; this is a courtesy bucket.
        return f"ip:{access_route[0]}"
    return f"ip:{remote_addr or 'unknown'}"


# --- rate limiting --------------------------------------------------------


class RateLimiter:
    """Sliding-window limiter over a short and a long window."""

    def __init__(self, per_min: int, per_day: int) -> None:
        self.per_min = per_min
        self.per_day = per_day
        self._lock = threading.Lock()
        self._minute: dict[str, deque[float]] = defaultdict(deque)
        self._day: dict[str, deque[float]] = defaultdict(deque)
        self._last_sweep = time.monotonic()

    @property
    def enabled(self) -> bool:
        return self.per_min > 0 or self.per_day > 0

    def check(self, identity: str) -> None:
        if not self.enabled:
            return

        now = time.monotonic()
        with self._lock:
            if self.per_min > 0:
                window = self._minute[identity]
                self._trim(window, now, 60)
                if len(window) >= self.per_min:
                    retry = max(1, int(60 - (now - window[0])))
                    raise rate_limited(f"{self.per_min}/min", retry)

            if self.per_day > 0:
                window = self._day[identity]
                self._trim(window, now, 86400)
                if len(window) >= self.per_day:
                    retry = max(1, int(86400 - (now - window[0])))
                    raise rate_limited(f"{self.per_day}/day", retry)

            if self.per_min > 0:
                self._minute[identity].append(now)
            if self.per_day > 0:
                self._day[identity].append(now)

            if now - self._last_sweep > 300:
                self._sweep(now)
                self._last_sweep = now

    @staticmethod
    def _trim(window: deque[float], now: float, span: float) -> None:
        cutoff = now - span
        while window and window[0] <= cutoff:
            window.popleft()

    def _sweep(self, now: float) -> None:
        """Drop identities with no recent activity so memory stays bounded."""
        for store, span in ((self._minute, 60), (self._day, 86400)):
            for identity in list(store.keys()):
                self._trim(store[identity], now, span)
                if not store[identity]:
                    del store[identity]

    def snapshot(self, identity: str) -> dict:
        if not self.enabled:
            return {"enabled": False}
        now = time.monotonic()
        with self._lock:
            minute = self._minute.get(identity, deque())
            day = self._day.get(identity, deque())
            self._trim(minute, now, 60)
            self._trim(day, now, 86400)
            return {
                "enabled": True,
                "used_this_minute": len(minute),
                "limit_per_minute": self.per_min or None,
                "used_today": len(day),
                "limit_per_day": self.per_day or None,
            }


# --- concurrency ----------------------------------------------------------


class ConcurrencyGate:
    """Caps simultaneous transfers so one instance cannot be swamped."""

    def __init__(self, limit: int, wait_sec: float = 2.0) -> None:
        self._semaphore = threading.BoundedSemaphore(limit)
        self._limit = limit
        self._wait = wait_sec
        self._active = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    @property
    def limit(self) -> int:
        return self._limit

    def __enter__(self) -> "ConcurrencyGate":
        # Brief wait absorbs bursts instead of rejecting them outright.
        if not self._semaphore.acquire(timeout=self._wait):
            raise ApiError(
                503,
                "server_busy",
                f"All {self._limit} transfer slots are in use.",
                hint="Retry shortly, raise MAX_CONCURRENT, or add instances.",
                retry_after=5,
            )
        with self._lock:
            self._active += 1
        return self

    def __exit__(self, *exc_info) -> None:
        with self._lock:
            self._active -= 1
        self._semaphore.release()


# --- CORS -----------------------------------------------------------------

_SAFE_REQUEST_HEADERS = "Content-Type, X-API-Key, Authorization, Range"
_EXPOSED_HEADERS = (
    "Content-Disposition, Content-Length, Content-Range, Accept-Ranges, "
    "X-Request-Id, X-Clip-Title, X-Clip-Duration, X-Clip-Platform, Retry-After"
)


def resolve_origin(cfg: Settings, origin: str | None) -> str | None:
    """Return the value for Access-Control-Allow-Origin, or None to omit it."""
    if not origin or not cfg.cors_origins:
        return None
    if "*" in cfg.cors_origins:
        return "*"
    for allowed in cfg.cors_origins:
        if origin.rstrip("/").lower() == allowed.rstrip("/").lower():
            return origin
    return None


def apply_cors(cfg: Settings, response, origin: str | None):
    allow = resolve_origin(cfg, origin)
    if allow is None:
        return response
    response.headers["Access-Control-Allow-Origin"] = allow
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = _SAFE_REQUEST_HEADERS
    response.headers["Access-Control-Expose-Headers"] = _EXPOSED_HEADERS
    response.headers["Access-Control-Max-Age"] = "600"
    if allow != "*":
        response.headers["Vary"] = "Origin"
    return response
