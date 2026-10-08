"""Classification tests for platform error messages.

The messages below are the real strings platforms emit, kept verbatim —
including typographic punctuation. That detail matters: yt-dlp relays
YouTube's "you’re" with U+2019, and a pattern written with an ASCII
apostrophe silently fails to match it. That is not a cosmetic bug. It made
every datacenter bot-check report as `auth_required` ("private or
age-restricted"), which sends an operator looking for a problem with the video
instead of a problem with their egress IP.
"""

from __future__ import annotations

import sys

from core.errors import from_ytdlp

# (message, expected code)
CASES: list[tuple[str, str]] = [
    # The bot check, exactly as yt-dlp relays it. Note U+2019 in "you’re".
    (
        "ERROR: [youtube] qhSS6i9JG7c: Sign in to confirm you\u2019re not a bot. "
        "Use --cookies-from-browser or --cookies for the authentication.",
        "blocked_by_platform",
    ),
    # Same thing with an ASCII apostrophe.
    (
        "Sign in to confirm you're not a bot. Use --cookies for the authentication.",
        "blocked_by_platform",
    ),
    # And the no-contraction phrasing.
    ("Sign in to confirm you are not a bot.", "blocked_by_platform"),
    ("You are suspected as a bot. Wait, or pass the captcha on the site.", "blocked_by_platform"),
    ("Our systems have detected unusual traffic from your computer network.", "blocked_by_platform"),
    # Age gate is a different fix (cookies), so it must not be lumped in.
    (
        "ERROR: [youtube] abc: Sign in to confirm your age. "
        "This video may be inappropriate for some users.",
        "age_restricted",
    ),
    ("Video is age restricted", "age_restricted"),
    # Genuinely private or members-only.
    ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access.", "auth_required"),
    ("ERROR: [youtube] abc: Join this channel to get access to members-only content", "auth_required"),
    (
        "ERROR: [instagram] XYZ: Requested content is not available, "
        "rate-limited or login required",
        "auth_required",
    ),
    # Other categories.
    ("ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader", "media_unavailable"),
    ("ERROR: Unsupported URL: https://example.com/not-a-video", "unsupported_url"),
    ("ERROR: [youtube] abc: The uploader has not made this video available in your country", "geo_blocked"),
    ("ERROR: Unable to download webpage: HTTP Error 429: Too Many Requests", "upstream_rate_limited"),
    ("ERROR: Requested format is not available", "no_suitable_format"),
    ("ERROR: Postprocessing: ffprobe/ffmpeg not found. Please install or provide the path", "ffmpeg_missing"),
    ("ERROR: Unable to download webpage: The read operation timed out", "upstream_timeout"),
    ("ERROR: [youtube] abc: This video is no longer available due to a copyright claim", "legally_blocked"),
    # Anything unrecognised must still be reported, not swallowed.
    ("ERROR: something entirely new and unseen", "extraction_failed"),
]


def main() -> int:
    passed = failed = 0
    print()
    for message, expected in CASES:
        actual = from_ytdlp(Exception(message)).code
        ok = actual == expected
        if ok:
            passed += 1
        else:
            failed += 1
        label = "PASS" if ok else "FAIL"
        preview = " ".join(message.split())[:62]
        print(f"  {label}  {actual:22s} {preview}")
        if not ok:
            print(f"        expected {expected!r}")

    # Normalisation must not mangle the message shown to the operator.
    err = from_ytdlp(Exception("Sign in to confirm you\u2019re not a bot."))
    has_text = "not a bot" in err.message
    has_hint = bool(err.hint) and "IMPERSONATE" in err.hint
    print(f"\n  {'PASS' if has_text else 'FAIL'}  platform message preserved: {err.message!r}")
    print(f"  {'PASS' if has_hint else 'FAIL'}  hint names a concrete remedy")
    passed += int(has_text) + int(has_hint)
    failed += int(not has_text) + int(not has_hint)

    print("\n" + "=" * 68)
    print(f"  passed: {passed}   failed: {failed}")
    print("=" * 68 + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
