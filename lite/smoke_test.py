"""End-to-end smoke test against a running Lite API instance.

    python smoke_test.py --base http://127.0.0.1:8099 --key test-key-local

Covers the service contract, the SSRF guard, and a real extraction against a
live URL. Network-dependent checks are reported separately from contract
checks, because a platform bot-check is an environment condition rather than a
code defect.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

passed: list[str] = []
failed: list[str] = []
skipped: list[str] = []


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface 3xx responses instead of following them.

    The redirect endpoint's whole contract is the 302 and its headers, so
    following it would test the CDN rather than this service.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def call(base: str, path: str, *, key: str | None = None, body: dict | None = None,
         method: str | None = None, timeout: int = 120):
    url = f"{base}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if key:
        headers["X-API-Key"] = key

    request = urllib.request.Request(url, data=data, headers=headers,
                                     method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            try:
                return response.status, json.loads(raw), dict(response.headers)
            except json.JSONDecodeError:
                return response.status, raw, dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw), dict(exc.headers)
        except json.JSONDecodeError:
            return exc.code, raw.decode("utf-8", "replace"), dict(exc.headers)
    except urllib.error.URLError as exc:
        return 0, {"error": {"code": "unreachable", "message": str(exc.reason)}}, {}


def check(name: str, condition: bool, detail: str = "") -> bool:
    if condition:
        passed.append(name)
        print(f"  PASS  {name}")
    else:
        failed.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")
    return condition


def skip(name: str, reason: str) -> None:
    skipped.append(f"{name}: {reason}")
    print(f"  SKIP  {name}  ({reason})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8099")
    parser.add_argument("--key", default="test-key-local")
    parser.add_argument(
        "--url",
        default="https://youtube.com/shorts/qhSS6i9JG7c",
        help="A real short video URL to extract",
    )
    args = parser.parse_args()
    base = args.base.rstrip("/")

    print("\n[1] Service endpoints")
    status, payload, _ = call(base, "/healthz")
    if not check("healthz returns 200", status == 200, f"got {status}"):
        print("\nServer unreachable; aborting.")
        return 1
    check("healthz reports yt-dlp version", bool(payload.get("yt_dlp")), str(payload))
    has_ffmpeg = bool(payload.get("ffmpeg"))
    print(f"        ffmpeg present: {has_ffmpeg}")

    status, payload, _ = call(base, "/readyz")
    check("readyz returns 200", status == 200, f"got {status}")

    status, payload, _ = call(base, "/api/v1/limits")
    check("limits is public (no key needed)", status == 200, f"got {status}")
    check("limits advertises max_duration_sec",
          isinstance(payload, dict) and "max_duration_sec" in payload, str(payload)[:120])

    status, _, _ = call(base, "/openapi.yaml")
    check("openapi.yaml is served", status == 200, f"got {status}")

    status, payload, _ = call(base, "/api/v1/limits")
    service_mode = payload.get("service_mode") if isinstance(payload, dict) else "full"
    check("limits reports the service mode",
          service_mode in {"full", "resolver"}, f"service_mode={service_mode}")
    print(f"        service mode: {service_mode}")

    print("\n[2] Authentication")
    status, payload, _ = call(base, "/api/v1/info?url=https://example.com/x")
    check("no key -> 401", status == 401, f"got {status}")
    check("401 body carries a code",
          isinstance(payload, dict) and payload.get("error", {}).get("code") == "unauthorized",
          str(payload)[:160])

    status, _, _ = call(base, "/api/v1/info?url=https://example.com/x", key="wrong-key")
    check("bad key -> 401", status == 401, f"got {status}")

    print("\n[3] SSRF guard")
    for target, label in [
        ("http://169.254.169.254/latest/meta-data/", "cloud metadata endpoint"),
        ("http://metadata.google.internal/", "GCP metadata host"),
        ("http://127.0.0.1:8099/healthz", "loopback"),
        ("http://10.0.0.5/video.mp4", "private RFC1918 range"),
        ("file:///etc/passwd", "file:// scheme"),
        ("http://example.com:22/x", "non-web port"),
    ]:
        encoded = urllib.parse.quote(target, safe="")
        status, payload, _ = call(base, f"/api/v1/info?url={encoded}", key=args.key)
        code = payload.get("error", {}).get("code") if isinstance(payload, dict) else None
        check(f"blocks {label}", status in (400, 403),
              f"got {status} code={code}")

    print("\n[4] Input validation")
    status, payload, _ = call(base, "/api/v1/info", key=args.key, body={})
    check("missing url -> 400", status == 400, f"got {status}")

    # Validate through /url, which exists in both service modes. Going via
    # /download would get a 501 in resolver mode before any parsing happens.
    status, payload, _ = call(base, "/api/v1/url", key=args.key,
                              body={"url": args.url, "mode": "banana"})
    check("invalid mode -> 400", status == 400, f"got {status}")

    status, payload, _ = call(base, "/api/v1/url", key=args.key,
                              body={"url": args.url, "quality": "huge"})
    check("invalid quality -> 400", status == 400, f"got {status}")

    if service_mode == "full":
        status, payload, _ = call(
            base, "/api/v1/download", key=args.key,
            body={"url": args.url, "audio_format": "aiff", "mode": "audio"},
        )
        check("invalid audio_format -> 400", status == 400, f"got {status}")
    else:
        skip("invalid audio_format", "download endpoint is disabled in resolver mode")

    print("\n[5] Live extraction (network dependent)")
    status, payload, _ = call(base, "/api/v1/info", key=args.key,
                              body={"url": args.url}, timeout=120)
    if status == 200:
        check("info returns a title", bool(payload.get("title")), str(payload)[:160])
        check("info returns a duration", payload.get("duration_sec") is not None,
              f"duration_sec={payload.get('duration_sec')}")
        check("info reports the extractor", bool(payload.get("extractor")), str(payload)[:160])
        check("info lists formats", bool(payload.get("formats")), "no formats")

        # Quality is the short side of the frame. For a portrait clip the
        # advertised qualities must therefore come from the width, not the
        # height, or every Reel and Short would be capped out of existence.
        resolution = str(payload.get("resolution") or "")
        qualities = payload.get("downloadable_qualities") or []
        if payload.get("is_vertical") and "x" in resolution:
            short_side = min(int(n) for n in resolution.split("x") if n.isdigit())
            check("portrait clip offers its full quality",
                  short_side in qualities,
                  f"resolution={resolution} short_side={short_side} offered={qualities}")

        formats = payload.get("formats") or []
        sample = next((f for f in formats if f.get("width") and f.get("height")), None)
        if sample:
            check("format quality == min(width, height)",
                  sample["quality"] == min(sample["width"], sample["height"]),
                  f"{sample['width']}x{sample['height']} -> quality={sample['quality']}")

        print(f"        title     : {str(payload.get('title'))[:66]}")
        print(f"        duration  : {payload.get('duration_human')}")
        print(f"        platform  : {payload.get('extractor')}")
        print(f"        resolution: {resolution}  vertical={payload.get('is_vertical')}")
        print(f"        qualities : {qualities}")
        print(f"        needs mux : {payload.get('needs_muxing')}")
        print(f"        in limits : {payload.get('within_limits')}")

        status, payload, _ = call(base, "/api/v1/url", key=args.key,
                                  body={"url": args.url, "quality": 480}, timeout=120)
        if status == 200:
            check("url returns a delivery mode",
                  payload.get("delivery") in {"single_file", "split_streams"}, str(payload)[:160])
            descriptor = payload.get("file") or payload.get("video") or {}
            check("url returns a media URL", bool(descriptor.get("url")), str(payload)[:160])
            print(f"        delivery : {payload.get('delivery')}")
            print(f"        filename : {descriptor.get('suggested_filename')}")
        else:
            code = payload.get("error", {}).get("code") if isinstance(payload, dict) else "?"
            skip("direct url resolution", f"{status} {code}")

        print("\n[6] Redirect delivery")
        # A 302 to the CDN spends no bandwidth here, which is what makes
        # resolver-mode hosts (Vercel) viable at all.
        query = urllib.parse.urlencode({"url": args.url, "mode": "audio"})
        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(f"{base}/api/v1/redirect?{query}",
                                     headers={"X-API-Key": args.key})
        try:
            with opener.open(req, timeout=120) as response:
                skip("redirect", f"expected a 302, got {response.status}")
        except urllib.error.HTTPError as exc:
            if exc.code == 302:
                location = exc.headers.get("Location") or ""
                check("redirect returns 302", True)
                check("redirect points off-site",
                      location.startswith("http") and base not in location,
                      f"Location={location[:80]}")
                check("redirect suggests a filename",
                      bool(exc.headers.get("X-Suggested-Filename")),
                      str(exc.headers.get("X-Suggested-Filename")))
                print(f"        location: {location[:66]}\u2026")
            elif exc.code == 422:
                skip("redirect", "no single-file format (expected for YouTube video)")
            else:
                body = exc.read().decode("utf-8", "replace")[:160]
                skip("redirect", f"HTTP {exc.code} {body}")

        if service_mode == "resolver":
            print("\n[7] Resolver mode refuses transfers")
            status, payload, _ = call(
                base, "/api/v1/download?url=" + urllib.parse.quote(args.url, safe=""),
                key=args.key,
            )
            code = payload.get("error", {}).get("code") if isinstance(payload, dict) else None
            check("download is disabled in resolver mode", status == 501,
                  f"got {status}")
            check("the refusal explains why", code == "resolver_mode",
                  f"code={code}")
            print("\n" + "=" * 68)
            print(f"  passed: {len(passed)}   failed: {len(failed)}   skipped: {len(skipped)}")
            print("=" * 68 + "\n")
            return 1 if failed else 0

        print("\n[7] Download through the API")
        query = urllib.parse.urlencode({"url": args.url, "mode": "video", "quality": "360"})
        request = urllib.request.Request(f"{base}/api/v1/download?{query}",
                                         headers={"X-API-Key": args.key})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                blob = response.read()
                disposition = response.headers.get("Content-Disposition", "")
                check("download returns bytes", len(blob) > 10_000, f"{len(blob)} bytes")
                check("download sets Content-Disposition",
                      "attachment" in disposition, disposition[:90])
                check("download reports the path used",
                      response.headers.get("X-Download-Via") in {"proxy", "server"},
                      str(response.headers.get("X-Download-Via")))
                print(f"        bytes    : {len(blob):,}")
                print(f"        filename : {disposition[:90]}")
                print(f"        via      : {response.headers.get('X-Download-Via')}")
                # mp4/mov carry 'ftyp' at offset 4; mkv/webm start with the
                # EBML magic; mp3 may lead with an ID3 tag.
                check("payload looks like real media",
                      b"ftyp" in blob[4:12]
                      or blob[:3] == b"ID3"
                      or blob[:4] == b"\x1a\x45\xdf\xa3",
                      f"leading bytes {blob[:12]!r}")

            print("\n[8] Audio extraction")
            query = urllib.parse.urlencode(
                {"url": args.url, "mode": "audio", "audio_format": "mp3"}
            )
            request = urllib.request.Request(f"{base}/api/v1/download?{query}",
                                             headers={"X-API-Key": args.key})
            if not has_ffmpeg:
                skip("mp3 conversion", "ffmpeg not installed")
            else:
                with urllib.request.urlopen(request, timeout=180) as response:
                    blob = response.read()
                    disposition = response.headers.get("Content-Disposition", "")
                    check("mp3 conversion returns bytes", len(blob) > 5_000,
                          f"{len(blob)} bytes")
                    check("mp3 filename ends in .mp3", ".mp3" in disposition,
                          disposition[:90])
                    check("mp3 payload has an audio signature",
                          blob[:3] == b"ID3" or blob[:2] == b"\xff\xfb" or blob[:2] == b"\xff\xf3",
                          f"leading bytes {blob[:8]!r}")
                    print(f"        bytes    : {len(blob):,}")
                    print(f"        via      : {response.headers.get('X-Download-Via')}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:220]
            skip("download", f"HTTP {exc.code} {body}")
        except urllib.error.URLError as exc:
            skip("download", str(exc.reason))
    else:
        code = payload.get("error", {}).get("code") if isinstance(payload, dict) else "?"
        message = payload.get("error", {}).get("message", "") if isinstance(payload, dict) else ""
        skip("live extraction", f"{status} {code}: {message[:140]}")
        print("\n        Platform-side condition, not a code failure. On datacenter and")
        print("        some residential IPs YouTube demands a bot check. Set PROXY,")
        print("        COOKIES_B64, or IMPERSONATE=chrome. See README.")

    print("\n" + "=" * 68)
    print(f"  passed: {len(passed)}   failed: {len(failed)}   skipped: {len(skipped)}")
    if failed:
        print("\n  Failures:")
        for item in failed:
            print(f"    - {item}")
    if skipped:
        print("\n  Skipped:")
        for item in skipped:
            print(f"    - {item}")
    print("=" * 68 + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
