"""yt-dlp wrapper: metadata extraction, format selection, server-side download."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

import yt_dlp

from .config import Settings
from .errors import ApiError, bad_request, deadline_exceeded, from_ytdlp, too_large

log = logging.getLogger("clipapi.extractor")

AUDIO_FORMATS = ("original", "mp3", "m4a", "opus", "wav", "flac")
VIDEO_CONTAINERS = ("auto", "mp4", "mkv", "webm")
MODES = ("video", "audio")

# Formats that are not real media (YouTube storyboards and the like).
_JUNK_EXTS = {"mhtml"}
_DIRECT_PROTOCOLS = {"http", "https"}


class AbortedDownload(Exception):
    """Raised from a progress hook to stop yt-dlp mid-download."""


class _NullLogger:
    """yt-dlp logger that routes everything to our own logger at debug level."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp warning: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp error: %s", msg)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _parse_extractor_args(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("EXTRACTOR_ARGS is not valid JSON (%s); ignoring it", exc)
        return {}
    if not isinstance(parsed, dict):
        log.warning("EXTRACTOR_ARGS must be a JSON object; ignoring it")
        return {}
    return parsed


def base_opts(cfg: Settings) -> dict:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "logger": _NullLogger(),
        "socket_timeout": cfg.socket_timeout,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 1,
        "geo_bypass": True,
        # Containers are ephemeral; a cache dir would just grow in RAM on Cloud Run.
        "cachedir": False,
        "check_formats": False,
        "ignoreerrors": False,
        "consoletitle": False,
        "call_home": False,
    }
    if cfg.proxy:
        opts["proxy"] = cfg.proxy
    if cfg.cookies_file:
        opts["cookiefile"] = cfg.cookies_file
    if cfg.user_agent:
        opts["http_headers"] = {"User-Agent": cfg.user_agent}
    extractor_args = _parse_extractor_args(cfg.extractor_args)
    if extractor_args:
        opts["extractor_args"] = extractor_args

    impersonate = os.environ.get("IMPERSONATE", "").strip()
    if impersonate:
        try:
            from yt_dlp.networking.impersonate import ImpersonateTarget

            opts["impersonate"] = ImpersonateTarget.from_str(impersonate)
        except Exception as exc:  # pragma: no cover - optional dependency
            log.warning("IMPERSONATE=%r could not be applied: %s", impersonate, exc)
    return opts


# --- extraction -----------------------------------------------------------


def extract(url: str, cfg: Settings) -> dict:
    """Fetch metadata without downloading. Raises ApiError on failure."""
    opts = base_opts(cfg)
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except ApiError:
        raise
    except Exception as exc:  # yt-dlp raises many types
        raise from_ytdlp(exc) from exc

    if info is None:
        raise ApiError(502, "extraction_failed", "The extractor returned no data.")

    info = _unwrap_playlist(info)

    if not cfg.allow_generic_extractor and _is_generic(info):
        raise ApiError(
            400,
            "unsupported_url",
            "No dedicated extractor matched this link.",
            hint=(
                "Only recognised platforms are served. Set ALLOW_GENERIC_EXTRACTOR=true "
                "to also accept bare media URLs and arbitrary web pages."
            ),
        )
    return info


def _unwrap_playlist(info: dict) -> dict:
    """Lite handles one clip, so reduce a playlist to its first playable entry."""
    if info.get("_type") not in {"playlist", "multi_video"}:
        return info

    entries: Iterable = info.get("entries") or []
    for entry in entries:
        if isinstance(entry, dict) and entry:
            entry = dict(entry)
            entry["_from_playlist"] = {
                "playlist_title": info.get("title"),
                "playlist_count": info.get("playlist_count") or len(list(entries) or []),
                "note": "This link is a playlist/profile. Only the first item was resolved. "
                "Use the Pro version for whole playlists.",
            }
            return entry

    raise ApiError(
        404,
        "media_unavailable",
        "That link is a playlist or profile with no playable items.",
    )


def _is_generic(info: dict) -> bool:
    key = (info.get("extractor_key") or info.get("extractor") or "").lower()
    return key.startswith("generic")


def guard(info: dict, cfg: Settings) -> None:
    """Enforce the "lite" contract: short, non-live, bounded media only."""
    if info.get("is_live"):
        if not cfg.allow_live:
            raise ApiError(
                422,
                "live_not_supported",
                "This is a live stream, which has no fixed length.",
                hint="Live capture needs the Pro version.",
            )
    if info.get("live_status") in {"is_upcoming"}:
        raise ApiError(422, "media_unavailable", "This media is scheduled but has not started.")

    duration = info.get("duration")
    if duration is None:
        # Unknown length: allowed, but the size cap and deadline still apply.
        log.info("duration unknown for %s; relying on size cap", info.get("id"))
        return

    try:
        duration = float(duration)
    except (TypeError, ValueError):
        return

    if duration > cfg.max_duration_sec:
        raise too_large(
            f"Clip is {_human_duration(duration)} long; this service accepts up to "
            f"{_human_duration(cfg.max_duration_sec)}.",
            hint=(
                "This is the Lite service, sized for Reels/Shorts/TikTok. Raise "
                "MAX_DURATION_SEC, or use the Pro version for full-length video."
            ),
        )


def _human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


# --- format selection -----------------------------------------------------


def _usable(fmt: dict) -> bool:
    if not fmt.get("url"):
        return False
    if fmt.get("ext") in _JUNK_EXTS:
        return False
    # Reject only when both tracks are *explicitly* absent. A missing value
    # means "not probed", which is different, and is what several extractors
    # return for a plain media URL.
    return not (fmt.get("vcodec") == "none" and fmt.get("acodec") == "none")


def _is_direct(fmt: dict) -> bool:
    protocol = (fmt.get("protocol") or "").lower()
    return protocol in _DIRECT_PROTOCOLS


def _height(fmt: dict) -> int | None:
    height = fmt.get("height")
    if isinstance(height, (int, float)) and height > 0:
        return int(height)
    resolution = fmt.get("resolution") or ""
    match = re.match(r"^\d+x(\d+)$", str(resolution))
    if match:
        return int(match.group(1))
    return None


def _width(fmt: dict) -> int | None:
    width = fmt.get("width")
    if isinstance(width, (int, float)) and width > 0:
        return int(width)
    resolution = fmt.get("resolution") or ""
    match = re.match(r"^(\d+)x\d+$", str(resolution))
    if match:
        return int(match.group(1))
    return None


def quality_of(fmt: dict) -> int | None:
    """The clip's quality in the colloquial sense: the short side of the frame.

    "1080p" means 1920x1080 landscape and 1080x1920 portrait alike. Measuring
    on height instead would treat a portrait 1080x1920 Reel as 1920p and cap it
    out, which would reject the best version of most Reels, Shorts and TikToks.
    """
    width, height = _width(fmt), _height(fmt)
    if width and height:
        return min(width, height)
    return height or width


def _bitrate(fmt: dict) -> float:
    for key in ("tbr", "vbr", "abr"):
        value = fmt.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return 0.0


def size_of(fmt: dict) -> int | None:
    for key in ("filesize", "filesize_approx"):
        value = fmt.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


_VIDEO_EXTS = {"mp4", "webm", "mkv", "mov", "flv", "avi", "3gp", "ts", "m4v", "mpg"}
_AUDIO_EXTS = {"m4a", "mp3", "opus", "ogg", "oga", "aac", "wav", "flac", "weba", "mka"}


def kind_of(fmt: dict) -> str:
    """Classify a format as video+audio, video-only or audio-only.

    yt-dlp draws a distinction that matters here: ``'none'`` means the track is
    definitely absent, while a missing or ``None`` value means the extractor
    never probed it. Extractors that skip probing (the generic one on a bare
    .mp4 URL, among others) leave both unknown. Treating unknown as absent
    would label a plain MP4 audio-only and make it unrelayable, so unknown
    falls back to what the container implies.
    """
    vcodec, acodec = fmt.get("vcodec"), fmt.get("acodec")
    has_video = bool(vcodec) and vcodec != "none"
    has_audio = bool(acodec) and acodec != "none"

    if has_video:
        # A genuinely video-only stream is flagged acodec='none'. Unknown here
        # means unprobed, so assume the file is complete.
        return "video+audio" if (has_audio or not acodec) else "video-only"
    if has_audio:
        return "audio-only"

    # Neither track is known. Judge by the container extension.
    ext = (fmt.get("ext") or "").lower()
    if ext in _AUDIO_EXTS:
        return "audio-only"
    # An unclassifiable media URL is most likely a complete file.
    return "video+audio"


def _video_score(fmt: dict, cap: int):
    """Rank video formats: inside the cap first, then biggest, then mp4."""
    quality = quality_of(fmt) or 0
    within_cap = quality <= cap  # unknown dimensions (0) count as within
    return (
        1 if within_cap else 0,
        quality if within_cap else -quality,
        1 if (fmt.get("ext") == "mp4") else 0,
        _bitrate(fmt),
    )


def pick_progressive(info: dict, cap: int) -> dict | None:
    """Best single file containing both video and audio.

    Returns None more often than you would expect. YouTube has largely retired
    progressive formats, so Shorts typically arrive as separate video-only and
    audio-only streams that must be muxed. Instagram and TikTok still serve
    combined files.
    """
    candidates = [
        fmt
        for fmt in info.get("formats") or []
        if _usable(fmt) and _is_direct(fmt) and kind_of(fmt) == "video+audio"
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda fmt: _video_score(fmt, cap))


def pick_audio(info: dict, *, require_direct: bool = True) -> dict | None:
    """Best audio-only stream, falling back to a progressive file."""
    formats = [
        fmt
        for fmt in info.get("formats") or []
        if _usable(fmt) and (_is_direct(fmt) or not require_direct)
    ]
    audio_only = [fmt for fmt in formats if kind_of(fmt) == "audio-only"]

    # m4a and mp3 play everywhere; opus and webm need a capable player.
    ext_rank = {"m4a": 3, "mp3": 3, "opus": 2, "webm": 2, "ogg": 2}

    if audio_only:
        return max(
            audio_only,
            key=lambda fmt: (ext_rank.get(fmt.get("ext") or "", 1), _bitrate(fmt)),
        )

    progressive = [fmt for fmt in formats if kind_of(fmt) == "video+audio"]
    if progressive:
        # The smallest progressive file still carries the full audio track, so
        # grabbing the lowest video quality wastes the least bandwidth.
        return min(progressive, key=lambda fmt: (quality_of(fmt) or 9999, _bitrate(fmt)))
    return None


def pick_video_only(info: dict, cap: int, *, require_direct: bool = True) -> dict | None:
    """Best video-only stream within the quality cap.

    ``require_direct`` keeps the result to plain http(s) so it can be relayed or
    handed to a client as-is. Drop it for the server path, where ffmpeg can
    also assemble HLS and DASH.
    """
    candidates = [
        fmt
        for fmt in info.get("formats") or []
        if _usable(fmt)
        and kind_of(fmt) == "video-only"
        and (_is_direct(fmt) or not require_direct)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda fmt: _video_score(fmt, cap))


def effective_quality(cfg: Settings, requested: str | int | None) -> int:
    """Clamp a caller's quality request to the service cap.

    Interpreted as the short side of the frame, so 720 means 720p whether the
    clip is landscape or portrait.
    """
    if requested in (None, "", "best", "max"):
        return cfg.max_quality
    try:
        value = int(str(requested).lower().rstrip("p"))
    except ValueError as exc:
        raise bad_request(
            f"Invalid quality {requested!r}.",
            hint="Use 360, 480, 720, 1080, or 'best'.",
        ) from exc
    if value <= 0:
        raise bad_request("quality must be a positive number.")
    return min(value, cfg.max_quality)


# --- public JSON shaping --------------------------------------------------


def format_summary(fmt: dict) -> dict:
    return {
        "format_id": fmt.get("format_id"),
        "ext": fmt.get("ext"),
        "kind": kind_of(fmt),
        # 'quality' is the short side and is what the API's quality parameter
        # compares against. 'height' and 'width' are the raw frame dimensions.
        "quality": quality_of(fmt),
        "height": _height(fmt),
        "width": _width(fmt),
        "fps": fmt.get("fps"),
        "vcodec": fmt.get("vcodec"),
        "acodec": fmt.get("acodec"),
        "tbr_kbps": fmt.get("tbr"),
        "abr_kbps": fmt.get("abr"),
        "filesize_bytes": size_of(fmt),
        "protocol": fmt.get("protocol"),
        "direct_downloadable": _is_direct(fmt),
        "note": fmt.get("format_note"),
    }


def summarize(info: dict, cfg: Settings, *, include_formats: bool = True) -> dict:
    description = info.get("description") or ""
    payload: dict[str, Any] = {
        "id": info.get("id"),
        "title": info.get("title"),
        "description": description[:500] + ("\u2026" if len(description) > 500 else ""),
        "duration_sec": info.get("duration"),
        "duration_human": _human_duration(info["duration"]) if info.get("duration") else None,
        "uploader": info.get("uploader") or info.get("channel"),
        "uploader_url": info.get("uploader_url") or info.get("channel_url"),
        "upload_date": info.get("upload_date"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "platform": (info.get("extractor_key") or "").lower() or None,
        "is_live": bool(info.get("is_live")),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "resolution": info.get("resolution"),
        "is_vertical": _is_vertical(info),
        # True when nothing can be downloaded as one file, so video and audio
        # must be muxed. Normal for YouTube; means ffmpeg is required.
        "needs_muxing": pick_progressive(info, cfg.max_quality) is None,
    }

    if info.get("_from_playlist"):
        payload["playlist"] = info["_from_playlist"]

    if include_formats:
        formats = [
            format_summary(fmt) for fmt in (info.get("formats") or []) if _usable(fmt)
        ]
        formats.sort(key=lambda f: (f["kind"], f["quality"] or 0, f["tbr_kbps"] or 0))
        payload["formats"] = formats

        qualities = sorted(
            {
                f["quality"]
                for f in formats
                if f["quality"] and f["kind"] in {"video+audio", "video-only"}
            }
        )
        payload["available_qualities"] = qualities
        payload["downloadable_qualities"] = [q for q in qualities if q <= cfg.max_quality]

    return payload


def _is_vertical(info: dict) -> bool | None:
    width, height = info.get("width"), info.get("height")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        return height > width
    for fmt in info.get("formats") or []:
        w, h = _width(fmt), _height(fmt)
        if w and h:
            return h > w
    return None


def headers_for(info: dict, fmt: dict) -> dict:
    """Request headers required to fetch a format's URL directly."""
    headers = dict(info.get("http_headers") or {})
    headers.update(fmt.get("http_headers") or {})
    # Hop-by-hop and stateful headers must not be replayed by a client.
    for drop in ("Cookie", "Accept-Encoding", "Connection", "Host", "Content-Length"):
        headers.pop(drop, None)
    return headers


# --- filenames ------------------------------------------------------------

_UNSAFE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def safe_filename(title: str | None, ext: str | None, *, fallback: str = "clip") -> str:
    stem = _UNSAFE.sub("_", (title or "").strip()) or fallback
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    if stem.lower() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    stem = stem[:120].strip(" .") or fallback
    suffix = re.sub(r"[^A-Za-z0-9]", "", ext or "") or "bin"
    return f"{stem}.{suffix}"


# --- server-side download (the ffmpeg path) -------------------------------


def build_selector(info: dict, mode: str, cap: int, container: str) -> str:
    """Build a yt-dlp format selector.

    Prefers explicit format ids chosen by our own ranking, so the short-side
    quality rule applies identically on the proxy and server paths. yt-dlp's
    own ``height<=N`` filter cannot express this: on a portrait 1080x1920 clip
    it reads the height as 1920 and would discard the best version.

    Falls back to a filter-based selector when a format cannot be identified,
    for example on extractors that expose no usable format list up front.
    """
    if mode == "audio":
        audio = pick_audio(info, require_direct=False)
        if audio and audio.get("format_id"):
            return f"{audio['format_id']}/bestaudio/best"
        return "bestaudio/best"

    progressive = pick_progressive(info, cap)
    video = pick_video_only(info, cap, require_direct=False)
    audio = pick_audio(info, require_direct=False)

    options: list[str] = []
    # Muxing video-only + audio-only gives the best result when available.
    if video and audio and video.get("format_id") and audio.get("format_id"):
        options.append(f"{video['format_id']}+{audio['format_id']}")
    if progressive and progressive.get("format_id"):
        options.append(str(progressive["format_id"]))

    # Generic fallbacks. The height filter is imprecise for portrait video but
    # only ever applies when explicit selection failed.
    if container == "mp4":
        options.append(f"bestvideo[height<={cap}][ext=mp4]+bestaudio[ext=m4a]")
    options.append(f"bestvideo[height<={cap}]+bestaudio")
    options.append("bestvideo+bestaudio")
    options.append("best")
    return "/".join(options)


def server_download(
    url: str,
    cfg: Settings,
    *,
    info: dict,
    mode: str = "video",
    quality: int | None = None,
    container: str = "mp4",
    audio_format: str = "mp3",
    workdir: Path,
    deadline: float,
) -> tuple[Path, dict]:
    """Download (and if needed transcode or mux) into ``workdir``.

    ``info`` is the already-extracted metadata, reused so format selection
    matches what /api/v1/info reported and so the extraction is not repeated.

    Returns the finished file and its info dict. The caller owns ``workdir``
    and must delete it once the response has been written.
    """
    quality = quality or cfg.max_quality
    have_ffmpeg = ffmpeg_available()

    if mode == "audio" and audio_format != "original" and not have_ffmpeg:
        raise ApiError(
            500,
            "ffmpeg_missing",
            f"Converting to {audio_format} requires ffmpeg, which is not installed.",
            hint="Use audio_format=original, or deploy the provided Dockerfile, which includes ffmpeg.",
        )

    # Muxing is the common case on YouTube, which has largely retired
    # progressive formats. Fail early with a clear reason rather than letting
    # yt-dlp abort partway through two downloads.
    if (
        mode == "video"
        and not have_ffmpeg
        and pick_progressive(info, quality) is None
    ):
        raise ApiError(
            500,
            "ffmpeg_missing",
            "This video is served as separate video and audio streams, which must be "
            "muxed, and ffmpeg is not installed.",
            hint=(
                "Deploy the provided Dockerfile, which includes ffmpeg. YouTube almost "
                "always needs this; Instagram and TikTok usually do not. Alternatively "
                "use mode=audio with audio_format=original, which needs no muxing."
            ),
        )

    state: dict[str, Any] = {"abort": None, "bytes": 0}

    def progress_hook(status: dict) -> None:
        if time.monotonic() > deadline:
            state["abort"] = "deadline"
            raise AbortedDownload("deadline exceeded")

        downloaded = status.get("downloaded_bytes") or 0
        total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
        state["bytes"] = max(state["bytes"], downloaded)

        if total and total > cfg.max_filesize_bytes:
            state["abort"] = "size"
            state["size"] = total
            raise AbortedDownload("declared size over cap")
        if downloaded > cfg.max_filesize_bytes:
            state["abort"] = "size"
            state["size"] = downloaded
            raise AbortedDownload("downloaded size over cap")

    opts = base_opts(cfg)
    opts.update(
        {
            "format": build_selector(info, mode, quality, container),
            "outtmpl": {"default": str(workdir / "%(id)s.%(ext)s")},
            "paths": {"home": str(workdir)},
            "progress_hooks": [progress_hook],
            "max_filesize": cfg.max_filesize_bytes,
            "overwrites": True,
            "continuedl": False,
            "windowsfilenames": True,
            "writethumbnail": False,
            "writesubtitles": False,
        }
    )

    postprocessors: list[dict] = []
    if mode == "audio":
        if audio_format != "original":
            postprocessors.append(
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": audio_format,
                    "preferredquality": "0" if audio_format == "mp3" else None,
                    "nopostoverwrites": False,
                }
            )
    else:
        if container in {"mp4", "mkv", "webm"}:
            opts["merge_output_format"] = container
    if postprocessors:
        opts["postprocessors"] = postprocessors

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except AbortedDownload as exc:
        raise _abort_error(state, cfg) from exc
    except ApiError:
        raise
    except Exception as exc:
        if state["abort"]:
            raise _abort_error(state, cfg) from exc
        raise from_ytdlp(exc) from exc

    if info is None:
        raise ApiError(502, "extraction_failed", "Download produced no result.")
    info = _unwrap_playlist(info)

    path = _finished_file(info, workdir)
    if path is None:
        raise ApiError(502, "download_failed", "The downloaded file could not be located.")

    actual = path.stat().st_size
    if actual > cfg.max_filesize_bytes:
        path.unlink(missing_ok=True)
        raise too_large(
            f"Result is {actual / 1048576:.1f} MB, over the {cfg.max_filesize_mb} MB cap.",
            hint="Request a lower 'quality', or raise MAX_FILESIZE_MB.",
        )
    return path, info


def _abort_error(state: dict, cfg: Settings) -> ApiError:
    if state.get("abort") == "deadline":
        return deadline_exceeded(cfg.soft_deadline_sec)
    size = state.get("size")
    detail = f" ({size / 1048576:.1f} MB)" if isinstance(size, (int, float)) else ""
    return too_large(
        f"Media exceeds the {cfg.max_filesize_mb} MB cap{detail}.",
        hint="Request a lower 'quality', switch to mode=audio, or raise MAX_FILESIZE_MB.",
    )


def _finished_file(info: dict, workdir: Path) -> Path | None:
    """Locate the final artefact, accounting for merges and postprocessing."""
    for download in info.get("requested_downloads") or []:
        for key in ("filepath", "_filename", "filename"):
            value = download.get(key)
            if value and Path(value).is_file():
                return Path(value)

    for key in ("filepath", "_filename"):
        value = info.get(key)
        if value and Path(value).is_file():
            return Path(value)

    # Last resort: newest real file in the working directory.
    files = [p for p in workdir.rglob("*") if p.is_file() and not p.name.endswith(".part")]
    if files:
        return max(files, key=lambda p: p.stat().st_mtime)
    return None
