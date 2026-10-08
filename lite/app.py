"""Clip Download API - Lite.

A stateless HTTP API that resolves and downloads short videos and audio from
YouTube (incl. Shorts), Instagram (Reels/posts), TikTok, X, Facebook and the
other ~1800 sites yt-dlp supports.

Built to run as a container on AWS App Runner, Google Cloud Run, ECS Fargate,
Lightsail Containers, Fly.io or plain Docker. No database, no queue, no disk
state: every request is self-contained, which is what lets the platform scale
it to zero and back.

Three endpoints do the real work:

  POST /api/v1/info      metadata and the list of available formats
  POST /api/v1/url       the direct, time-limited media URL (no bytes proxied)
  GET  /api/v1/download  the media itself, relayed through this service

Prefer /api/v1/url when the caller can fetch the media itself: it sidesteps
request-duration caps entirely and costs you no egress. Use /api/v1/download
when the media host requires headers the client cannot set, or when you need
server-side audio conversion.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    make_response,
    redirect,
    request,
    send_file,
)
from werkzeug.exceptions import HTTPException

from core import config, extractor, streaming, urlcheck
from core.cache import TTLCache
from core.errors import ApiError, bad_request
from core.security import (
    ConcurrencyGate,
    RateLimiter,
    apply_cors,
    authenticate,
    caller_id,
)

APP_VERSION = "1.0.0"
API_PREFIX = "/api/v1"
BASE_DIR = Path(__file__).resolve().parent


# --- logging --------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """One JSON object per line: readable in CloudWatch and Cloud Logging."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        for key in ("path", "status", "duration_ms", "platform", "mode", "bytes", "code"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if os.environ.get("LOG_FORMAT", "json").lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    # Werkzeug's own access log duplicates ours.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


log = logging.getLogger("clipapi")


# --- helpers --------------------------------------------------------------


def _params() -> dict:
    """Merge query string and JSON body into one parameter dict."""
    values: dict = {}
    if request.is_json:
        body = request.get_json(silent=True)
        if body is None:
            raise bad_request("Request body is not valid JSON.")
        if not isinstance(body, dict):
            raise bad_request("Request body must be a JSON object.")
        values.update(body)
    elif request.form:
        values.update(request.form.to_dict())
    for key, value in request.args.items():
        values.setdefault(key, value)
    return values


def _require_url(values: dict, cfg: config.Settings) -> str:
    raw = values.get("url") or values.get("link") or values.get("u")
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if not isinstance(raw, (str, type(None))):
        raise bad_request("'url' must be a string.")
    return urlcheck.validate(
        raw or "",
        guard=cfg.ssrf_guard,
        allowed_domains=cfg.allowed_domains,
        blocked_domains=cfg.blocked_domains,
    )


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _content_disposition(filename: str, *, inline: bool = False) -> str:
    """RFC 6266 header with an ASCII fallback plus a UTF-8 variant."""
    disposition = "inline" if inline else "attachment"
    ascii_name = filename.encode("ascii", "ignore").decode("ascii").strip() or "clip"
    ascii_name = ascii_name.replace('"', "")
    return (
        f"{disposition}; filename=\"{ascii_name}\"; "
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )


def _ascii_header(value: str | None, limit: int = 200) -> str:
    """Percent-encode a value so it survives as a latin-1 HTTP header."""
    if not value:
        return ""
    return quote(str(value)[:limit], safe=" ._-()[]")


def create_app() -> Flask:
    cfg = config.load()
    _setup_logging(cfg.log_level)

    for warning in cfg.warnings:
        log.warning(warning)

    has_ffmpeg = extractor.ffmpeg_available()
    if not has_ffmpeg:
        log.warning(
            "ffmpeg not found on PATH: audio conversion and stream merging are disabled. "
            "Direct and proxied downloads of single-file formats still work. The provided "
            "Dockerfile installs ffmpeg."
        )

    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.update(
        MAX_CONTENT_LENGTH=64 * 1024,  # requests are tiny JSON bodies
        SEND_FILE_MAX_AGE_DEFAULT=0,
        PROPAGATE_EXCEPTIONS=False,
    )
    # Preserve the order fields are built in; it reads far better than
    # alphabetical. In Flask 3 this lives on app.json, not app.config.
    app.json.sort_keys = False

    cache = TTLCache(cfg.cache_ttl_sec, cfg.cache_max_entries)
    limiter = RateLimiter(cfg.rate_per_min, cfg.rate_per_day)
    gate = ConcurrencyGate(cfg.max_concurrent)
    started_at = time.time()

    app.extensions["clipapi"] = {
        "cfg": cfg,
        "cache": cache,
        "limiter": limiter,
        "gate": gate,
        "has_ffmpeg": has_ffmpeg,
    }

    _start_janitor(cfg, cache)

    # --- request lifecycle ------------------------------------------------

    @app.before_request
    def _begin():
        g.request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:16]
        g.started = time.monotonic()
        g.api_key = None

        if request.method == "OPTIONS":
            return apply_cors(cfg, make_response("", 204), request.headers.get("Origin"))

        if request.path in {"/healthz", "/readyz", "/favicon.ico"}:
            return None
        if request.path == "/" or request.path.startswith("/static/"):
            return None
        if request.path in {f"{API_PREFIX}/limits", "/openapi.yaml"}:
            return None

        if request.path.startswith(API_PREFIX):
            g.api_key = authenticate(cfg, request.headers, request.args)
            identity = caller_id(cfg, g.api_key, request.access_route, request.remote_addr)
            g.identity = identity
            limiter.check(identity)
        return None

    @app.after_request
    def _finish(response: Response):
        response.headers["X-Request-Id"] = getattr(g, "request_id", "-")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.path.startswith(API_PREFIX):
            response.headers.setdefault("Cache-Control", "no-store")
        apply_cors(cfg, response, request.headers.get("Origin"))

        started = getattr(g, "started", None)
        if started is not None and request.path not in {"/healthz", "/readyz"}:
            log.info(
                "%s %s -> %s",
                request.method,
                request.path,
                response.status_code,
                extra={
                    "request_id": getattr(g, "request_id", None),
                    "path": request.path,
                    "status": response.status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                },
            )
        return response

    # --- error handling ---------------------------------------------------

    @app.errorhandler(ApiError)
    def _api_error(exc: ApiError):
        payload = exc.to_dict()
        payload["error"]["request_id"] = getattr(g, "request_id", None)
        response = jsonify(payload)
        response.status_code = exc.status
        if exc.retry_after is not None:
            response.headers["Retry-After"] = str(exc.retry_after)
        log.info(
            "request rejected: %s",
            exc.message,
            extra={"request_id": getattr(g, "request_id", None), "code": exc.code},
        )
        return response

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        response = jsonify(
            {
                "error": {
                    "code": exc.name.lower().replace(" ", "_"),
                    "message": exc.description,
                    "request_id": getattr(g, "request_id", None),
                }
            }
        )
        response.status_code = exc.code or 500
        return response

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception):
        log.exception(
            "unhandled error", extra={"request_id": getattr(g, "request_id", None)}
        )
        response = jsonify(
            {
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected error occurred.",
                    "request_id": getattr(g, "request_id", None),
                }
            }
        )
        response.status_code = 500
        return response

    # --- infrastructure endpoints ----------------------------------------

    @app.get("/healthz")
    def healthz():
        """Liveness. Point your platform's HTTP health check here."""
        return jsonify(
            {
                "status": "ok",
                "version": APP_VERSION,
                "yt_dlp": _ytdlp_version(),
                "ffmpeg": has_ffmpeg,
                "uptime_sec": round(time.time() - started_at, 1),
            }
        )

    @app.get("/readyz")
    def readyz():
        ready = not (cfg.require_api_key and not cfg.api_keys)
        payload = {
            "status": "ready" if ready else "misconfigured",
            "active_transfers": gate.active,
            "max_concurrent": gate.limit,
            "cache": cache.stats(),
        }
        if not ready:
            payload["reason"] = "REQUIRE_API_KEY is on but no API_KEYS are configured."
        return jsonify(payload), (200 if ready else 503)

    @app.get(f"{API_PREFIX}/limits")
    def limits():
        """Advertise the service contract so clients can validate before calling."""
        payload = cfg.public_limits()
        payload.update(
            {
                "version": APP_VERSION,
                "yt_dlp": _ytdlp_version(),
                "ffmpeg": has_ffmpeg,
                "modes": list(extractor.MODES),
                "audio_formats": list(extractor.AUDIO_FORMATS)
                if has_ffmpeg
                else ["original"],
                "video_containers": list(extractor.VIDEO_CONTAINERS)
                if has_ffmpeg
                else ["auto"],
                "active_transfers": gate.active,
            }
        )
        return jsonify(payload)

    @app.get("/openapi.yaml")
    def openapi():
        spec = BASE_DIR / "openapi.yaml"
        if not spec.is_file():
            raise ApiError(404, "not_found", "openapi.yaml is not bundled in this image.")
        return send_file(spec, mimetype="application/yaml")

    @app.get("/")
    def index():
        if not cfg.enable_ui:
            return jsonify(
                {
                    "service": "clip-download-api-lite",
                    "version": APP_VERSION,
                    "docs": "/openapi.yaml",
                    "service_mode": cfg.service_mode,
                    "endpoints": [
                        f"{API_PREFIX}/limits",
                        f"{API_PREFIX}/info",
                        f"{API_PREFIX}/url",
                        f"{API_PREFIX}/redirect",
                    ]
                    + ([] if cfg.resolver_only else [f"{API_PREFIX}/download"]),
                }
            )
        from flask import render_template

        return render_template(
            "index.html",
            version=APP_VERSION,
            limits=cfg.public_limits(),
            auth_required=cfg.auth_enabled,
            has_ffmpeg=has_ffmpeg,
            resolver_only=cfg.resolver_only,
        )

    # --- API --------------------------------------------------------------

    def _resolve(url: str) -> dict:
        """Extract metadata, served from the TTL cache when possible."""
        cached = cache.get(url)
        if cached is not None:
            return cached
        info = extractor.extract(url, cfg)
        cache.put(url, info)
        return info

    @app.route(f"{API_PREFIX}/info", methods=["GET", "POST"])
    def info_endpoint():
        values = _params()
        url = _require_url(values, cfg)
        info = _resolve(url)

        payload = extractor.summarize(
            info, cfg, include_formats=_as_bool(values.get("formats"), True)
        )
        payload["within_limits"] = True
        payload["limit_note"] = None
        try:
            extractor.guard(info, cfg)
        except ApiError as exc:
            payload["within_limits"] = False
            payload["limit_note"] = exc.message
        return jsonify(payload)

    @app.route(f"{API_PREFIX}/url", methods=["GET", "POST"])
    def url_endpoint():
        """Resolve the direct media URL without moving any bytes.

        This is the cheapest and most scalable path: the caller fetches the
        media straight from the platform CDN. Caveat: these URLs expire within
        minutes to hours, and many are bound to the resolving IP, so hand them
        to the client promptly and expect to re-resolve on failure.
        """
        values = _params()
        url = _require_url(values, cfg)
        mode = _mode(values)
        info = _resolve(url)
        extractor.guard(info, cfg)

        quality = extractor.effective_quality(cfg, values.get("quality"))
        payload: dict = {
            "media": extractor.summarize(info, cfg, include_formats=False),
            "mode": mode,
            "expires": "Media URLs are short-lived (typically minutes to hours) and are "
            "often locked to the IP that resolved them.",
        }

        if mode == "audio":
            chosen = extractor.pick_audio(info)
            if chosen is None:
                raise ApiError(
                    422,
                    "no_suitable_format",
                    "No directly downloadable audio stream was found.",
                    hint=f"Try {API_PREFIX}/download?via=server, which can demux and convert.",
                )
            payload["delivery"] = "single_file"
            payload["file"] = _file_descriptor(info, chosen)
            return jsonify(payload)

        progressive = extractor.pick_progressive(info, quality)
        if progressive is not None:
            payload["delivery"] = "single_file"
            payload["file"] = _file_descriptor(info, progressive)
            return jsonify(payload)

        video = extractor.pick_video_only(info, quality)
        audio = extractor.pick_audio(info)
        if video is not None and audio is not None:
            payload["delivery"] = "split_streams"
            payload["video"] = _file_descriptor(info, video)
            payload["audio"] = _file_descriptor(info, audio)
            payload["note"] = (
                "Video and audio are separate streams here, which is normal for YouTube: "
                "it has largely retired combined formats. Mux them with ffmpeg on your "
                "side, or call /api/v1/download (via=auto) and this service will do it. "
                "Example: ffmpeg -i video.mp4 -i audio.m4a -c copy out.mp4"
            )
            return jsonify(payload)

        raise ApiError(
            422,
            "no_suitable_format",
            "No directly downloadable stream was found for this media.",
            hint=(
                "The media is probably HLS/DASH only. Use "
                f"{API_PREFIX}/download?via=server so ffmpeg can assemble it."
            ),
        )

    @app.route(f"{API_PREFIX}/redirect", methods=["GET"])
    def redirect_endpoint():
        """302 straight to the media on the platform CDN.

        The cheapest possible delivery: no bytes traverse this service, so
        response-size caps and request timeouts never apply. That is what makes
        the resolver mode usable on hosts like Vercel, which refuses any
        response body over 4.5 MB.

        Two limits are inherent, not bugs. The CDN URL is often bound to the IP
        that resolved it, so a browser following this redirect may get a 403;
        and a platform with no combined video+audio format (YouTube) cannot be
        served this way at all, because muxing needs ffmpeg.
        """
        values = _params()
        url = _require_url(values, cfg)
        mode = _mode(values)
        quality = extractor.effective_quality(cfg, values.get("quality"))
        info = _resolve(url)
        extractor.guard(info, cfg)

        chosen = (
            extractor.pick_audio(info)
            if mode == "audio"
            else extractor.pick_progressive(info, quality)
        )
        if chosen is None or not chosen.get("url"):
            raise ApiError(
                422,
                "no_suitable_format",
                "This media has no single downloadable file, so it cannot be served "
                "by redirect.",
                hint=(
                    "Video and audio are separate streams and must be muxed, which "
                    "needs ffmpeg. Normal for YouTube. Use a full-mode deployment "
                    f"and call {API_PREFIX}/download, or request mode=audio."
                    if not cfg.resolver_only
                    else "Normal for YouTube. Deploy in full mode (Render, Cloud Run, "
                    "Docker) to have the service mux it, or request mode=audio."
                ),
            )

        target = chosen["url"]
        log.info(
            "redirecting to CDN",
            extra={"request_id": g.request_id, "mode": mode, "platform": info.get("extractor")},
        )
        response = redirect(target, code=302)
        # Browsers cannot set the Referer a CDN may require, so surface what the
        # media expects for callers that can.
        response.headers["X-Media-Headers"] = json.dumps(extractor.headers_for(info, chosen))
        response.headers["X-Suggested-Filename"] = _ascii_header(
            extractor.safe_filename(info.get("title"), chosen.get("ext") or "bin")
        )
        _clip_headers(info, response)
        return response

    @app.route(f"{API_PREFIX}/download", methods=["GET", "POST"])
    def download_endpoint():
        """Return the media bytes.

        via=proxy (default) relays from the platform without touching disk.
        via=server downloads with yt-dlp and runs ffmpeg, which is required for
        audio conversion and for merging split streams.
        """
        if cfg.resolver_only:
            raise ApiError(
                501,
                "resolver_mode",
                "This deployment runs in resolver mode and does not transfer media.",
                hint=(
                    f"Use {API_PREFIX}/url to get the CDN URL, or {API_PREFIX}/redirect "
                    "to be sent straight to it. Resolver mode exists for hosts that cap "
                    "response bodies (Vercel) or disallow ffmpeg; deploy in full mode on "
                    "Render, Cloud Run or Docker to transfer media through the service."
                ),
            )

        values = _params()
        url = _require_url(values, cfg)
        mode = _mode(values)
        via = str(values.get("via") or "auto").lower()
        if via not in {"proxy", "server", "auto"}:
            raise bad_request("'via' must be one of: proxy, server, auto.")

        audio_format = str(values.get("audio_format") or "original").lower()
        if audio_format not in extractor.AUDIO_FORMATS:
            raise bad_request(
                f"'audio_format' must be one of: {', '.join(extractor.AUDIO_FORMATS)}."
            )

        container = str(values.get("container") or "auto").lower()
        if container not in extractor.VIDEO_CONTAINERS:
            raise bad_request(
                f"'container' must be one of: {', '.join(extractor.VIDEO_CONTAINERS)}."
            )

        quality = extractor.effective_quality(cfg, values.get("quality"))
        info = _resolve(url)
        extractor.guard(info, cfg)

        def server() -> Response:
            return _server_response(
                cfg, gate, info, url, mode, quality, container, audio_format
            )

        # Conversion and container changes are ffmpeg work by definition.
        wants_processing = (mode == "audio" and audio_format != "original") or (
            mode == "video" and container != "auto"
        )
        # A single file we could relay byte-for-byte, if one exists. Often it
        # does not: YouTube has largely retired progressive formats, so video
        # requests there need muxing.
        single = (
            extractor.pick_audio(info)
            if mode == "audio"
            else extractor.pick_progressive(info, quality)
        )

        if via == "server":
            return server()

        if via == "proxy":
            if wants_processing:
                raise bad_request(
                    "That combination requires server-side processing.",
                    hint="Use via=server or via=auto, or drop audio_format/container.",
                )
            if single is None:
                raise ApiError(
                    422,
                    "no_suitable_format",
                    "No single file can be relayed: this media is served as separate "
                    "video and audio streams.",
                    hint="Use via=auto (the default) to let this service mux them.",
                )
            return _proxy_response(cfg, gate, info, single, mode)

        # via == auto: relay when we can, process when we must.
        if wants_processing or single is None:
            if not cfg.enable_server_download:
                raise ApiError(
                    422,
                    "no_suitable_format",
                    "This media needs server-side assembly, which is disabled here.",
                    hint="Set ENABLE_SERVER_DOWNLOAD=true, or request mode=audio.",
                )
            if not has_ffmpeg:
                raise ApiError(
                    500,
                    "ffmpeg_missing",
                    "This media needs ffmpeg to assemble, and ffmpeg is not installed.",
                    hint=(
                        "Deploy the provided Dockerfile, which includes ffmpeg. YouTube "
                        "video almost always needs it; Instagram and TikTok usually do not."
                    ),
                )
            log.info(
                "using server-side path",
                extra={
                    "request_id": g.request_id,
                    "mode": mode,
                    "code": "processing" if wants_processing else "muxing",
                },
            )
            return server()

        return _proxy_response(cfg, gate, info, single, mode)

    return app


# --- response builders ----------------------------------------------------


def _mode(values: dict) -> str:
    mode = str(values.get("mode") or "video").lower()
    if mode in {"a", "mp3", "sound"}:
        mode = "audio"
    if mode in {"v", "mp4"}:
        mode = "video"
    if mode not in extractor.MODES:
        raise bad_request(f"'mode' must be one of: {', '.join(extractor.MODES)}.")
    return mode


def _file_descriptor(info: dict, fmt: dict) -> dict:
    ext = fmt.get("ext") or "bin"
    return {
        "url": fmt.get("url"),
        "http_headers": extractor.headers_for(info, fmt),
        "suggested_filename": extractor.safe_filename(info.get("title"), ext),
        "format": extractor.format_summary(fmt),
    }


class _GuardedBody:
    """A WSGI response body that guarantees cleanup exactly once.

    Cleanup here means releasing the concurrency slot and deleting the temp
    directory, so failing to run it wedges the service: slots never come back
    and the instance stops accepting downloads until it restarts.

    Two traps make the obvious approaches unreliable:

    * ``Response.call_on_close`` does not fire for ``send_file`` responses.
      send_file sets ``direct_passthrough=True``, and Werkzeug's
      ``get_app_iter`` returns such a body unwrapped rather than inside the
      ``ClosingIterator`` that would invoke those callbacks.
    * A generator's ``finally`` block does not run if the generator never
      started. A client that disconnects before reading the first byte
      produces exactly that.

    What a WSGI server does reliably is call ``close()`` on the iterable it was
    handed, so cleanup lives there. Werkzeug's ``Response.close`` also
    delegates to this, which covers HEAD requests, where the body is replaced
    by an empty iterable before the server ever sees it. Both routes can fire,
    hence the idempotency guard.
    """

    def __init__(self, chunks, on_close, *, label: str = "transfer") -> None:
        self._chunks = iter(chunks)
        self._on_close = on_close
        self._label = label
        self._closed = False

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        return next(self._chunks)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        inner_close = getattr(self._chunks, "close", None)
        try:
            if inner_close is not None:
                inner_close()
        except Exception:  # pragma: no cover
            log.exception("error closing %s body", self._label)
        finally:
            try:
                self._on_close()
            except Exception:  # pragma: no cover
                log.exception("error during %s cleanup", self._label)


def _iter_file(path: Path, chunk_size: int, deadline: float):
    """Yield a file's bytes, abandoning the read past the deadline."""
    with path.open("rb") as handle:
        while True:
            if time.monotonic() > deadline:
                log.warning("file stream aborted at deadline: %s", path.name)
                return
            chunk = handle.read(chunk_size)
            if not chunk:
                return
            yield chunk


def _clip_headers(info: dict, response: Response) -> None:
    response.headers["X-Clip-Title"] = _ascii_header(info.get("title"))
    response.headers["X-Clip-Platform"] = _ascii_header(
        info.get("extractor_key") or info.get("extractor"), 60
    )
    if info.get("duration"):
        response.headers["X-Clip-Duration"] = str(int(float(info["duration"])))
    if info.get("id"):
        response.headers["X-Clip-Id"] = _ascii_header(info.get("id"), 80)


def _proxy_response(
    cfg: config.Settings,
    gate: ConcurrencyGate,
    info: dict,
    fmt: dict,
    mode: str,
) -> Response:
    media_url = fmt.get("url")
    if not media_url:
        raise ApiError(502, "no_suitable_format", "The chosen format carries no URL.")

    declared = extractor.size_of(fmt)
    if declared and declared > cfg.max_filesize_bytes:
        raise ApiError(
            413,
            "limit_exceeded",
            f"Media is about {declared / 1048576:.1f} MB, over the "
            f"{cfg.max_filesize_mb} MB cap.",
            hint="Request a lower 'quality', or raise MAX_FILESIZE_MB.",
        )

    def validator(candidate: str) -> str:
        return urlcheck.validate(
            candidate,
            guard=cfg.ssrf_guard,
            allowed_domains=(),  # CDN hosts differ from page hosts
            blocked_domains=cfg.blocked_domains,
        )

    gate.__enter__()
    try:
        upstream = streaming.open_upstream(
            media_url,
            headers=extractor.headers_for(info, fmt),
            range_header=request.headers.get("Range"),
            timeout=cfg.socket_timeout,
            proxy=cfg.proxy,
            validator=validator,
            max_bytes=cfg.max_filesize_bytes,
        )
    except BaseException:
        gate.__exit__(None, None, None)
        raise

    deadline = time.monotonic() + cfg.soft_deadline_sec
    request_id = getattr(g, "request_id", "-")

    def release() -> None:
        gate.__exit__(None, None, None)
        log.info("proxy stream finished", extra={"request_id": request_id, "mode": mode})

    body = _GuardedBody(
        upstream.iter_chunks(
            chunk_size=cfg.chunk_size,
            deadline=deadline,
            max_bytes=cfg.max_filesize_bytes,
        ),
        release,
        label="proxy",
    )

    ext = fmt.get("ext") or "bin"
    filename = extractor.safe_filename(info.get("title"), ext)
    content_type = (
        upstream.content_type
        or mimetypes.guess_type(filename)[0]
        or ("audio/mpeg" if mode == "audio" else "video/mp4")
    )

    response = Response(
        body,
        status=upstream.status,
        mimetype=content_type,
        direct_passthrough=True,
    )
    response.headers["Content-Disposition"] = _content_disposition(filename)
    response.headers["Accept-Ranges"] = "bytes"
    if upstream.content_length is not None:
        response.headers["Content-Length"] = str(upstream.content_length)
    if "Content-Range" in upstream.headers:
        response.headers["Content-Range"] = upstream.headers["Content-Range"]
    response.headers["X-Download-Via"] = "proxy"
    _clip_headers(info, response)
    return response


def _server_response(
    cfg: config.Settings,
    gate: ConcurrencyGate,
    info: dict,
    url: str,
    mode: str,
    quality: int,
    container: str,
    audio_format: str,
) -> Response:
    if not cfg.enable_server_download:
        raise ApiError(
            403,
            "server_download_disabled",
            "Server-side downloading is disabled on this deployment.",
            hint="Set ENABLE_SERVER_DOWNLOAD=true, or use via=proxy.",
        )

    if container == "auto":
        container = "mp4"
    if mode == "audio" and audio_format == "original":
        audio_format = "mp3" if extractor.ffmpeg_available() else "original"

    gate.__enter__()
    workdir: Path | None = None
    try:
        workdir = Path(tempfile.mkdtemp(prefix="dl-", dir=str(cfg.tmp_dir)))
        deadline = time.monotonic() + cfg.soft_deadline_sec
        path, result_info = extractor.server_download(
            url,
            cfg,
            info=info,
            mode=mode,
            quality=quality,
            container=container,
            audio_format=audio_format,
            workdir=workdir,
            deadline=deadline,
        )
        filename = extractor.safe_filename(
            result_info.get("title") or info.get("title"), path.suffix.lstrip(".")
        )
        size = path.stat().st_size
        request_id = getattr(g, "request_id", "-")
        target = workdir

        def cleanup() -> None:
            shutil.rmtree(target, ignore_errors=True)
            gate.__exit__(None, None, None)
            log.info(
                "server download cleaned up",
                extra={"request_id": request_id, "mode": mode},
            )

        # Streamed through _GuardedBody rather than send_file: send_file sets
        # direct_passthrough, which makes Werkzeug skip the ClosingIterator
        # that would run call_on_close, so the slot and the temp dir would
        # never be reclaimed.
        body = _GuardedBody(
            _iter_file(path, cfg.chunk_size, deadline),
            cleanup,
            label="server",
        )
        content_type = (
            mimetypes.guess_type(filename)[0]
            or ("audio/mpeg" if mode == "audio" else "video/mp4")
        )
        response = Response(body, mimetype=content_type, direct_passthrough=True)
        response.headers["Content-Length"] = str(size)
        response.headers["Content-Disposition"] = _content_disposition(filename)
        # The file is built per request and deleted as the response closes, so
        # there is nothing for a client to range-request against later.
        response.headers["Accept-Ranges"] = "none"
        response.headers["X-Download-Via"] = "server"
        _clip_headers(result_info or info, response)
        return response
    except BaseException:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
        gate.__exit__(None, None, None)
        raise


# --- background maintenance ----------------------------------------------


def _start_janitor(cfg: config.Settings, cache: TTLCache) -> None:
    """Safety net for cache entries and temp dirs that outlived their request.

    Each request already cleans up after itself; this catches the cases where a
    worker was killed mid-flight. On Cloud Run, CPU is throttled between
    requests, so treat this as best-effort rather than a schedule.
    """

    def loop() -> None:
        while True:
            time.sleep(120)
            try:
                cache.purge_expired()
                _purge_stale_tempdirs(cfg.tmp_dir, max_age_sec=max(600, cfg.soft_deadline_sec * 4))
            except Exception:  # pragma: no cover
                log.exception("janitor pass failed")

    threading.Thread(target=loop, name="janitor", daemon=True).start()


def _purge_stale_tempdirs(root: Path, *, max_age_sec: int) -> None:
    if not root.is_dir():
        return
    cutoff = time.time() - max_age_sec
    for child in root.iterdir():
        if not child.is_dir() or not child.name.startswith("dl-"):
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                log.info("removed stale temp dir %s", child)
        except OSError:
            continue


def _ytdlp_version() -> str:
    try:
        from yt_dlp.version import __version__

        return __version__
    except Exception:  # pragma: no cover
        return "unknown"


app = create_app()


if __name__ == "__main__":
    settings = app.extensions["clipapi"]["cfg"]
    # Default to all interfaces because that is required inside a container.
    # Set HOST=127.0.0.1 to run it as a personal app, reachable only from this
    # machine; that is the one case where REQUIRE_API_KEY=false is defensible.
    host = os.environ.get("HOST", "0.0.0.0").strip() or "0.0.0.0"  # noqa: S104
    if host == "0.0.0.0" and not settings.auth_enabled:  # noqa: S104
        log.warning(
            "SECURITY: listening on all interfaces with no API key. Anyone who can "
            "reach this port can use it. Set HOST=127.0.0.1 for local-only use, or "
            "set API_KEYS."
        )
    # Development server only. Containers run gunicorn (see Dockerfile).
    app.run(
        host=host,
        port=settings.port,
        debug=_as_bool(os.environ.get("FLASK_DEBUG"), False),
        threaded=True,
    )
