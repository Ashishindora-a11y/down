# Clip Download API — Lite

A stateless HTTP API that resolves and downloads short videos and audio from YouTube (including Shorts), Instagram (Reels and posts), TikTok, X, Facebook and the other ~1800 sites [yt-dlp](https://github.com/yt-dlp/yt-dlp) supports.

Built to be called by another application. One container image, no database, no queue, no persistent disk, so it scales to zero and back on a managed host.

For full-length video, playlists, 4K and a local media library, use the Pro version instead.

---

## Pick the right endpoint

This matters more than anything else in this README.

| Endpoint | Bytes through the service | Use when |
|---|---|---|
| `GET /api/v1/redirect` | none | A `302` to the CDN. Put it behind a link and the browser does the rest. |
| `POST /api/v1/url` | none | **Default for an API client.** You want the URL plus the headers it needs. |
| `GET /api/v1/download` | all of them | The CDN needs headers your client cannot send, or the media needs muxing or conversion. |

`/api/v1/url` hands back the platform's own CDN URL plus the headers needed to fetch it. The service spends no bandwidth and no request time, so platform request caps stop mattering. Three caveats: those URLs expire within minutes to hours, many are bound to the IP that resolved them, and on YouTube you will usually get `delivery: split_streams` (separate video and audio to mux yourself). The bundled clients handle all three — they detect a CDN refusal or a split response and fall back to the download endpoint.

---

## Run it locally

```powershell
cd lite
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:API_KEYS = "downytube"
python app.py
```

Open <http://localhost:8080> for the test console, or call the API:

```powershell
curl -X POST http://localhost:8080/api/v1/info `
  -H "X-API-Key: dev-local-key" -H "Content-Type: application/json" `
  -d '{\"url\":\"https://youtube.com/shorts/VIDEO_ID\"}'
```

ffmpeg is needed for YouTube video and for audio conversion (see below). Install it with `winget install Gyan.FFmpeg`, then restart your shell. Or use Docker, where it is already present:

```powershell
docker compose up --build
```

### Verify a deployment

[`smoke_test.py`](smoke_test.py) exercises the service contract, the SSRF guard and a real extraction. Point it at any instance, local or deployed:

```powershell
.\.venv\Scripts\python.exe smoke_test.py --base https://your-service.onrender.com --key YOUR_KEY
```

It detects the service mode and asserts the matching contract, and separates contract failures from network-dependent skips so a platform bot-check does not read as a code defect.

Two more focused scripts:

- [`leak_probe.py`](leak_probe.py) — checks that every delivery path returns its concurrency slot, by reading `active_transfers` from `/readyz` before and after. Worth running after touching response handling: a leaked slot permanently reduces capacity and eventually wedges the instance.
- [`test_relay.py`](test_relay.py) — relay correctness, HTTP Range forwarding, CORS and rate limiting, against a local origin it starts itself. Needs `ALLOW_GENERIC_EXTRACTOR=true` and `SSRF_GUARD=false`.

---

## Two service modes

| | `SERVICE_MODE=full` (default) | `SERVICE_MODE=resolver` |
|---|---|---|
| Needs | Docker or a shell with ffmpeg | Plain Python, no binaries |
| Media bytes | Relayed or muxed by the service | Never touch the service |
| YouTube video | Works | **Cannot work** |
| YouTube audio, Instagram, TikTok, X | Works | Works |
| Endpoints | all | `/info`, `/url`, `/redirect` |

Resolver mode exists for hosts that cap response bodies or forbid native binaries — Vercel above all, where function responses are capped at 4.5 MB and there is no ffmpeg. It is not a crippled build: it answers metadata queries and returns a `302` to the platform CDN, which is the fastest delivery path there is. It just cannot combine two streams into one file.

Full host-by-host analysis and step-by-step instructions: **[DEPLOYER.md](DEPLOYER.md)**.

## Deploy

| Host | Mode | Notes |
|---|---|---|
| Cloud Run | full | Best overall. 3600 s timeouts, scales to zero. |
| Render | full | Best free option. `render.yaml` is committed. |
| Koyeb | full | One free 512 MB instance. |
| Vercel | resolver | Excellent as a resolver. No YouTube video. |
| Your own VPS | full | No limits, no cold starts, better IP reputation. |
| PythonAnywhere free | — | **Cannot work**: outbound allowlist blocks the platforms. |
| Pella free | — | **Not viable**: 100 MB RAM. |

### Google Cloud Run (recommended)

```powershell
cd lite\deploy
.\gcloud-run.ps1 -Project my-project -Region us-central1
```

Cloud Run is the better host for this workload: request timeout up to 3600s, scales to zero, and still open to new customers. The script enables the required APIs, stores your API key in Secret Manager, builds via Cloud Build (so ffmpeg is included) and prints the service URL. It deploys **private** by default; add `-AllowUnauthenticated` to expose it publicly.

One quirk to respect: Cloud Run's filesystem is RAM-backed, so anything written to `/tmp` consumes the memory you are billed for. The default download path never writes to disk.

### AWS App Runner

```powershell
cd lite\deploy
.\aws-apprunner.ps1 -Region ap-south-1
```

Two hard constraints, neither of which this code can work around:

- **App Runner closed to new customers on 2026-03-31.** If your account has never created an App Runner service, creation fails. Existing customers are fine.
- **120-second request timeout**, covering request read and full response write, not configurable. `SOFT_DEADLINE_SEC` defaults to 110 so you get a clean `504` instead of a severed connection. For anything slower, use `/api/v1/url`.

The script builds for `linux/amd64` (App Runner rejects arm64), pushes to ECR, and creates or updates the service. It checks for the ECR access role but will not create it: that is an account-wide permissions change, so it prints the exact commands and stops.

### Render (best free option)

Push to GitHub, then in Render pick **New +** → **Blueprint**. It reads `render.yaml` at the repository root, builds the Dockerfile so ffmpeg is included, and generates an API key for you.

Free tier spins down after 15 minutes idle (~1 minute cold start) and allows 5 GB egress per month, so prefer `/api/v1/redirect` which spends none of it.

### Vercel (resolver mode)

```powershell
npm i -g vercel
cd lite
vercel --prod
```

Set **Root Directory** to `lite` in the project settings. `api/index.py` forces resolver mode, since full mode cannot work there.

### Anywhere else

The image is plain and portable — it reads `$PORT` and serves HTTP. ECS Fargate, Lightsail Containers, Railway and a bare Docker host all work with no changes. Fly.io no longer has a free allowance.

---

## Configuration

Full annotated list in [`.env.example`](.env.example). The ones that matter:

| Variable | Default | Notes |
|---|---|---|
| `API_KEYS` | *(empty)* | Comma-separated. Empty + `REQUIRE_API_KEY=true` rejects everything, by design. |
| `REQUIRE_API_KEY` | `true` | `false` leaves the endpoint fully open. |
| `CORS_ORIGINS` | *(empty)* | Browser origins. `*` exposes your key in frontend source. |
| `MAX_DURATION_SEC` | `300` | The cap that makes this "lite". |
| `MAX_FILESIZE_MB` | `250` | |
| `MAX_QUALITY` | `1080` | Short side of the frame. See note below. |
| `SOFT_DEADLINE_SEC` | `110` | Keep under your platform's request cap. |
| `MAX_CONCURRENT` | `4` | Simultaneous transfers per instance. |
| `RATE_LIMIT_PER_MIN` | `20` | Per key, per instance. |
| `ENABLE_SERVER_DOWNLOAD` | `true` | Needs ffmpeg. Required for YouTube, see below. |
| `SSRF_GUARD` | `true` | Keep on. Blocks cloud metadata and internal IPs. |
| `PROXY` | *(empty)* | The main lever against platform bot checks. |
| `COOKIES_B64` | *(empty)* | Base64 of a Netscape `cookies.txt`, for private or age-gated media. |

### Quality is the short side of the frame

`quality=1080` means 1080p whether the clip is landscape (1920×1080) or portrait (1080×1920). Capping on height would be wrong here: Reels, Shorts and TikToks are portrait, so their best version is 1920 *tall*, and a height cap of 1080 would reject it. Each format reports `quality` (short side) alongside raw `width` and `height`.

### ffmpeg is required for YouTube

YouTube has largely retired combined video+audio formats. Verified against a real Short: 29 video-only streams, 10 audio-only, zero combined. So YouTube video always needs muxing, which means ffmpeg.

The default `via=auto` handles this transparently — it relays a single file when the platform has one and muxes when it does not. Consequences:

- The Docker image includes ffmpeg, so container deploys are fine.
- A bare `pip install` host without ffmpeg can do metadata, direct URLs, Instagram/TikTok downloads and `audio_format=original`, but **not** YouTube video.
- `/api/v1/info` tells you up front via `needs_muxing`.

### Security notes

- Every `/api/v1` route requires an API key, and the default fails closed. Comparison is constant-time.
- Rate limits and the concurrency gate are in-process. With N instances the effective ceiling is N × the limit. They are guardrails against accidental hammering, not billing-grade quota; put API Gateway or Cloud Armor in front if you need hard enforcement.
- A service that fetches caller-supplied URLs is an SSRF primitive by nature. `SSRF_GUARD` restricts requests to http/https on ports 80/443 whose hostnames resolve to public unicast addresses, and re-validates every redirect hop. Leave it on.
- `COOKIES_B64` holds live session credentials. Store it in Secrets Manager or Secret Manager, never in git, and use a throwaway account rather than your own.
- The `?key=` query parameter exists so browser links work. It lands in access logs; prefer the `X-API-Key` header for server-to-server calls.

---

## Calling it from your app

Ready-made clients are in [`clients/`](clients/): [`clip_client.py`](clients/clip_client.py) and [`clipClient.js`](clients/clipClient.js). Both default to the direct-CDN path, fall back to the proxy path on refusal, write through a `.part` file so a failed transfer cannot leave a truncated file, and retry `429`/`503` with backoff while honouring `Retry-After`.

```python
from clip_client import ClipClient

client = ClipClient("https://clip-api-xxxx.run.app", api_key=os.environ["CLIP_API_KEY"])
meta = client.info("https://youtube.com/shorts/VIDEO_ID")
client.download_direct("https://youtube.com/shorts/VIDEO_ID", "clip.mp4")
```

For a private Cloud Run service, Cloud Run consumes the `Authorization` header for IAM, so pass the identity token separately and keep the app key in `X-API-Key`:

```python
from clip_client import for_private_cloud_run
client = for_private_cloud_run("https://clip-api-xxxx.run.app", api_key=os.environ["CLIP_API_KEY"])
```

Full schema: [`openapi.yaml`](openapi.yaml), also served at `GET /openapi.yaml`.

---

## Errors

Every failure returns the same JSON shape. Branch on `code`, never on the message.

```json
{
  "error": {
    "code": "blocked_by_platform",
    "message": "Sign in to confirm you're not a bot",
    "hint": "Datacenter IPs are challenged far more often than home IPs.",
    "request_id": "9f2c1ab4de77"
  }
}
```

| Code | Status | Meaning |
|---|---|---|
| `blocked_by_platform` | 403 | Bot check. See below. |
| `auth_required` | 403 | Private or age-gated. Needs cookies. |
| `geo_blocked` | 451 | Set `PROXY` to a permitted region. |
| `upstream_rate_limited` | 429 | Platform throttling this IP. |
| `limit_exceeded` | 413 | Over `MAX_DURATION_SEC` or `MAX_FILESIZE_MB`. |
| `no_suitable_format` | 422 | No single file to relay. Drop `via=proxy` and use the default. |
| `ffmpeg_missing` | 500 | Media needs muxing or conversion. Deploy the Docker image. |
| `resolver_mode` | 501 | This deployment does not transfer media. Use `/url` or `/redirect`. |
| `server_busy` | 503 | Concurrency gate full. Honour `Retry-After`. |
| `deadline_exceeded` | 504 | Over `SOFT_DEADLINE_SEC`. Use `/api/v1/url`. |
| `extraction_failed` | 502 | Usually a stale yt-dlp. Rebuild. |

`X-Request-Id` on every response matches the `request_id` in the logs, which are JSON lines on stdout and parse natively in CloudWatch and Cloud Logging.

### When YouTube says "Sign in to confirm you're not a bot"

Expect this on any cloud host. Datacenter IP ranges are challenged far more aggressively than residential ones, and it is a property of where your container runs, not a bug in this code. Roughly in order of effectiveness:

1. **A residential or mobile proxy** via `PROXY`. The single biggest improvement.
2. **Cookies** from a logged-in session via `COOKIES_B64`. Required for private and age-gated media regardless.
3. **`IMPERSONATE=chrome`** — `curl-cffi` ships in the image and makes the TLS fingerprint look like a browser.
4. **`EXTRACTOR_ARGS`** — switching the YouTube player client often clears it: `{"youtube":{"player_client":["android","web_safari"]}}`.

Instagram and TikTok are less aggressive for public content but need cookies for anything private.

---

## Keeping it working

yt-dlp goes stale. Platforms change their players and extraction breaks, which surfaces as `extraction_failed`. Rebuild and redeploy monthly:

```powershell
pip install --upgrade "yt-dlp[default]"
pip freeze | Select-String "yt-dlp|Flask|gunicorn"   # then update requirements.txt
```

---

## Limits worth knowing

- **One clip per call.** A playlist or profile URL resolves to its first item and says so in the `playlist` field of the response.
- **No live streams** unless you set `ALLOW_LIVE=true`; they have no fixed length, so the caps cannot apply.
- **No job queue.** Every request is synchronous. That is what keeps the service stateless and horizontally scalable; it is also why long media belongs in the Pro version.
- **Metadata cache is per process.** Multiple instances each keep their own.
- **Muxing costs time and memory.** The server path downloads two streams and runs ffmpeg before the first byte reaches the client, so it is slower than a relay and it does touch disk. Budget for it on App Runner's 120s ceiling, and remember Cloud Run bills `/tmp` as memory.

---

## Legal

You are responsible for what you download. Respect copyright and each platform's terms of service. Intended for media you own, media you have permission to use, or content licensed for reuse.
