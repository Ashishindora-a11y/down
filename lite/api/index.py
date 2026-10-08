"""Vercel entrypoint.

Vercel's Python runtime looks for a WSGI callable named ``app`` in this file
and routes requests to it (see ../vercel.json).

Resolver mode is forced here rather than left to configuration, because two
Vercel constraints make full mode impossible and a misconfigured env var would
produce confusing 500s instead of a clear error:

  * Response bodies are capped at 4.5 MB. Relaying a video through a function
    fails with FUNCTION_RESPONSE_PAYLOAD_TOO_LARGE for anything but a tiny
    clip, which is most clips.
  * The filesystem is read-only apart from /tmp and there is no ffmpeg, so
    muxing and audio conversion cannot run.

What does work, and works well: metadata lookups and CDN redirects. Both are
small JSON or a 302, so the client fetches media straight from the platform.

Consequence worth knowing before you deploy here: YouTube video will not work.
It has no combined video+audio format any more, so it always needs muxing.
Instagram, TikTok, X and Facebook serve single files and are fine.

Set the project's Root Directory to `lite` in Vercel's settings so this file
and requirements.txt are found.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the sibling package importable: Vercel runs this file from api/.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ["SERVICE_MODE"] = "resolver"
os.environ["ENABLE_SERVER_DOWNLOAD"] = "false"
# Only /tmp is writable, and it is small.
os.environ.setdefault("TMP_DIR", "/tmp")
# Each invocation may land on a cold instance, so an in-process cache rarely
# gets a second hit. Keep it small; it costs memory for little return.
os.environ.setdefault("CACHE_MAX_ENTRIES", "64")
# Functions are billed by duration and capped well under this anyway.
os.environ.setdefault("SOFT_DEADLINE_SEC", "45")
# Per-instance counters are near useless when instances are ephemeral. Use
# Vercel's own firewall/rate limiting for real enforcement.
os.environ.setdefault("RATE_LIMIT_PER_MIN", "0")
os.environ.setdefault("RATE_LIMIT_PER_DAY", "0")

from app import app  # noqa: E402  (import must follow the env setup above)

__all__ = ["app"]
