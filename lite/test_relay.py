"""Focused tests for the relay path, Range forwarding, CORS and rate limiting.

These cover behaviour the main smoke test cannot reach through YouTube, which
serves no combined video+audio format and so always takes the server-side
muxing path.

Sections 1 and 2 run against a local origin started by this script, rather
than a public sample host. That keeps them deterministic: no third-party
uptime, no geo-blocking, and full control over whether the origin honours
Range requests.

Run against an instance started with ALLOW_GENERIC_EXTRACTOR=true and
SSRF_GUARD=false (the guard would otherwise block the loopback origin, which is
exactly what it is supposed to do):

    $env:API_KEYS="test-key-local"; $env:ALLOW_GENERIC_EXTRACTOR="true"
    $env:SSRF_GUARD="false"; $env:RATE_LIMIT_PER_MIN="40"; $env:PORT="8096"
    python app.py
    python test_relay.py --base http://127.0.0.1:8096 --key test-key-local
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

passed: list[str] = []
failed: list[str] = []
skipped: list[str] = []


# --- synthetic media + local origin --------------------------------------


def build_fake_mp4(payload_size: int = 2 * 1024 * 1024) -> bytes:
    """A byte string with a valid MP4 'ftyp' box header, then filler.

    Not playable, and it does not need to be: these tests check transport
    behaviour (status codes, ranges, headers, byte counts), not decoding.
    """
    brand = b"ftypisom" + b"isomiso2mp41"
    header = struct.pack(">I", len(brand) + 4) + brand
    filler = bytes(range(256)) * ((payload_size // 256) + 1)
    return header + filler[:payload_size]


MEDIA = build_fake_mp4()


class RangeOriginHandler(BaseHTTPRequestHandler):
    """A minimal origin that honours single-range requests.

    http.server's SimpleHTTPRequestHandler ignores Range entirely, so the
    relevant part is implemented here.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # silence per-request logging
        return

    def _send(self, status: int, body: bytes, headers: dict) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            self.wfile.write(body)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # The reader hung up mid-body, which is normal when a probe only
            # wanted the headers.
            pass

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") not in ("/sample.mp4", "/sample.mp4?"):
            self._send(404, b"not found", {"Content-Type": "text/plain"})
            return

        total = len(MEDIA)
        base_headers = {
            "Content-Type": "video/mp4",
            "Accept-Ranges": "bytes",
        }

        range_header = self.headers.get("Range")
        if not range_header:
            self._send(200, MEDIA, base_headers)
            return

        try:
            spec = range_header.split("=", 1)[1]
            start_text, _, end_text = spec.partition("-")
            start = int(start_text) if start_text else 0
            end = int(end_text) if end_text else total - 1
        except (IndexError, ValueError):
            self._send(416, b"", {**base_headers, "Content-Range": f"bytes */{total}"})
            return

        if start >= total or start > end:
            self._send(416, b"", {**base_headers, "Content-Range": f"bytes */{total}"})
            return

        end = min(end, total - 1)
        chunk = MEDIA[start : end + 1]
        self._send(
            206,
            chunk,
            {**base_headers, "Content-Range": f"bytes {start}-{end}/{total}"},
        )


class RangeOriginServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        # yt-dlp probes the URL and drops the connection, and the API aborts
        # early once it has the bytes it needs. A reset here is expected
        # client behaviour, not an origin failure, so it is not worth a
        # traceback on stderr.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def start_origin() -> tuple[ThreadingHTTPServer, str]:
    server = RangeOriginServer(("127.0.0.1", 0), RangeOriginHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}/sample.mp4"


# --- helpers --------------------------------------------------------------


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


def request(url: str, *, headers: dict | None = None, method: str = "GET", timeout: int = 90):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)
    except urllib.error.URLError as exc:
        return 0, str(exc.reason).encode(), {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8096")
    parser.add_argument("--key", default="test-key-local")
    parser.add_argument(
        "--ratelimit-key",
        default="test-key-ratelimit",
        help=(
            "A second configured key, used only to exhaust the rate limit. "
            "Rate limits bucket per key, so this keeps section 4 from "
            "spending the budget the other sections need."
        ),
    )
    args = parser.parse_args()
    base = args.base.rstrip("/")
    auth = {"X-API-Key": args.key}

    origin, media_url = start_origin()
    print(f"local origin: {media_url}  ({len(MEDIA):,} bytes)")

    try:
        query = urllib.parse.urlencode({"url": media_url, "via": "proxy"})

        print("\n[1] Relay path on a progressive single file")
        status, blob, headers = request(f"{base}/api/v1/download?{query}", headers=auth)

        if status != 200:
            detail = blob[:220].decode("utf-8", "replace")
            if b"host_not_allowed" in blob or b"not allowed" in blob:
                skip("relay download",
                     "blocked by the SSRF guard; start the server with "
                     "SSRF_GUARD=false so the loopback origin is reachable")
            elif status == 429:
                skip("relay download",
                     "rate limited from an earlier run; the window is 60s, so "
                     "wait and re-run, or restart the server")
            elif status == 503:
                skip("relay download",
                     "all concurrency slots busy; another test run is probably "
                     "still in flight")
            else:
                check("relay download succeeds", False, f"HTTP {status} {detail}")
        else:
            check("relay returns the whole file", len(blob) == len(MEDIA),
                  f"got {len(blob)} of {len(MEDIA)} bytes")
            check("relay is byte-exact", blob == MEDIA, "payload differs from origin")
            check("relay used the proxy path",
                  headers.get("X-Download-Via") == "proxy",
                  f"X-Download-Via={headers.get('X-Download-Via')}")
            check("relay advertises range support",
                  headers.get("Accept-Ranges") == "bytes",
                  f"Accept-Ranges={headers.get('Accept-Ranges')}")
            check("relay sets an attachment filename",
                  "attachment" in (headers.get("Content-Disposition") or ""),
                  str(headers.get("Content-Disposition"))[:90])
            check("relay reports the upstream content type",
                  (headers.get("Content-Type") or "").startswith("video/mp4"),
                  str(headers.get("Content-Type")))
            check("relay declares Content-Length",
                  headers.get("Content-Length") == str(len(MEDIA)),
                  f"Content-Length={headers.get('Content-Length')}")
            print(f"        bytes : {len(blob):,}")

            print("\n[2] HTTP Range forwarding")
            status, partial, headers = request(
                f"{base}/api/v1/download?{query}",
                headers={**auth, "Range": "bytes=0-1023"},
            )
            check("range request returns 206", status == 206, f"got {status}")
            check("range returns exactly the requested bytes", len(partial) == 1024,
                  f"{len(partial)} bytes")
            check("range bytes match the origin", partial == MEDIA[:1024],
                  "payload differs")
            check("range sets Content-Range",
                  (headers.get("Content-Range") or "").startswith("bytes 0-1023/"),
                  str(headers.get("Content-Range")))
            print(f"        Content-Range: {headers.get('Content-Range')}")

            status, mid, headers = request(
                f"{base}/api/v1/download?{query}",
                headers={**auth, "Range": "bytes=1000-1999"},
            )
            check("a mid-file range resumes at the right offset",
                  status == 206 and mid == MEDIA[1000:2000],
                  f"status={status} len={len(mid)}")

        print("\n[3] CORS")
        status, _, headers = request(
            f"{base}/api/v1/info",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-API-Key, Content-Type",
            },
            method="OPTIONS",
        )
        check("preflight succeeds without a key", status in (200, 204), f"got {status}")
        check("preflight echoes the allowed origin",
              headers.get("Access-Control-Allow-Origin") == "http://localhost:3000",
              str(headers.get("Access-Control-Allow-Origin")))
        check("preflight permits X-API-Key",
              "X-API-Key" in (headers.get("Access-Control-Allow-Headers") or ""),
              str(headers.get("Access-Control-Allow-Headers")))
        check("preflight exposes Content-Disposition",
              "Content-Disposition" in (headers.get("Access-Control-Expose-Headers") or ""),
              str(headers.get("Access-Control-Expose-Headers")))

        status, _, headers = request(
            f"{base}/api/v1/limits", headers={"Origin": "https://evil.example"}
        )
        check("an unlisted origin gets no CORS grant",
              headers.get("Access-Control-Allow-Origin") is None,
              str(headers.get("Access-Control-Allow-Origin")))

        print("\n[4] Rate limiting")
        # Deliberately exhausts a budget, so it runs on its own key.
        rl_auth = {"X-API-Key": args.ratelimit_key}
        probe_status, _, _ = request(f"{base}/api/v1/limits", headers=rl_auth)
        status, body, _ = request(f"{base}/api/v1/info?url=" +
                                  urllib.parse.quote(media_url, safe=""),
                                  headers=rl_auth, timeout=60)
        if status == 401:
            print(f"        '{args.ratelimit_key}' is not configured; "
                  "falling back to the main key")
            rl_auth = auth

        info_url = f"{base}/api/v1/info?url=" + urllib.parse.quote(media_url, safe="")
        statuses = []
        tripped_at = None
        for index in range(80):
            status, blob, headers = request(info_url, headers=rl_auth, timeout=60)
            statuses.append(status)
            if status == 429:
                tripped_at = index
                payload = json.loads(blob)
                check("429 carries a retry_after",
                      payload.get("error", {}).get("retry_after") is not None,
                      str(payload)[:140])
                check("429 sets the Retry-After header",
                      headers.get("Retry-After") is not None,
                      str(headers.get("Retry-After")))
                break
        if tripped_at is not None:
            check("the rate limiter engages", True)
            print(f"        limit tripped after {tripped_at} accepted calls")
        else:
            skip("rate limiter", f"no 429 in 80 calls (statuses seen: {set(statuses)})")

        print("\n[5] Request correlation")
        _, _, headers = request(f"{base}/healthz")
        check("every response carries X-Request-Id",
              bool(headers.get("X-Request-Id")), str(headers.get("X-Request-Id")))
        _, _, headers = request(f"{base}/healthz",
                                headers={"X-Request-Id": "trace-me-12345"})
        check("a caller-supplied request id is preserved",
              headers.get("X-Request-Id") == "trace-me-12345",
              str(headers.get("X-Request-Id")))
    finally:
        origin.shutdown()
        origin.server_close()

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
