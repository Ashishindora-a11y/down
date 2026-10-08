"""Pinpoint which download path leaks a concurrency slot.

Reads active_transfers from /readyz before and after each delivery path, so a
slot that is acquired but never released shows up immediately.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8091"
KEY = sys.argv[2] if len(sys.argv) > 2 else "leak-test"
URL = "https://youtube.com/shorts/qhSS6i9JG7c"


import time


def active() -> int:
    with urllib.request.urlopen(f"{BASE}/readyz", timeout=15) as r:
        return json.load(r)["active_transfers"]


def settled(expected: int = 0, timeout: float = 8.0) -> int:
    """Wait for the slot count to reach ``expected``, then return it.

    A client finishes reading the body slightly before the WSGI server calls
    close() on it, and the slot is released in close(). Sampling immediately
    after the read therefore catches a slot that is about to be freed and
    reports a leak that is not real.
    """
    deadline = time.monotonic() + timeout
    while True:
        value = active()
        if value == expected or time.monotonic() > deadline:
            return value
        time.sleep(0.25)


def download(label: str, **params) -> None:
    before = active()
    query = urllib.parse.urlencode({"url": URL, **params})
    req = urllib.request.Request(
        f"{BASE}/api/v1/download?{query}", headers={"X-API-Key": KEY}
    )
    status, size, via = "?", 0, "?"
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            body = r.read()
            status, size, via = r.status, len(body), r.headers.get("X-Download-Via")
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.read()
    after = settled(before)
    flag = "LEAK" if after > before else "ok  "
    print(
        f"  {flag}  {label:34s} status={status} via={via or '-':6s} "
        f"bytes={size:>9,} active {before}->{after}"
    )


def main() -> int:
    print(f"\nprobing {BASE}")
    print(f"  baseline active_transfers: {active()}\n")

    # Audio with no conversion takes the proxy relay path: YouTube exposes a
    # direct audio-only stream, so there is a single file to stream.
    download("audio, original (proxy relay)", mode="audio", audio_format="original")

    # Video needs muxing on YouTube, so this goes through ffmpeg on the server.
    download("video 360p (server mux)", mode="video", quality="360")

    # Conversion forces the server path too.
    download("audio, mp3 (server convert)", mode="audio", audio_format="mp3")

    # An error after the gate is taken must still release it.
    download("quality over cap (error path)", mode="video", quality="99999")

    final = settled(0)
    print(f"\n  final active_transfers: {final}")
    print("  RESULT:", "CLEAN" if final == 0 else f"LEAKED {final} SLOT(S)")
    return 0 if final == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
