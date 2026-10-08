# Deploying Clipfetch

Where this runs well, where it runs badly, and where it cannot run at all — then step-by-step instructions for each.

All limits below were checked in October 2026. Free tiers change often; re-check before you commit.

---

## One thing decides everything: ffmpeg

YouTube no longer serves combined video+audio formats. Measured against a real Short: **29 video-only streams, 10 audio-only, zero combined.** So every YouTube video download needs the two streams muxed, and muxing needs ffmpeg.

That splits every host into two camps, and the app has a mode for each:

| | `SERVICE_MODE=full` | `SERVICE_MODE=resolver` |
|---|---|---|
| Needs | Docker or a shell with ffmpeg | Plain Python, no binaries |
| Media bytes | Relayed or muxed by the service | Never touch the service |
| YouTube video | Works | **Cannot work** |
| YouTube audio | Works | Works |
| Instagram, TikTok, X, Facebook | Works | Works |
| Endpoints | all | `/info`, `/url`, `/redirect` |

Resolver mode is not a crippled build. It answers metadata queries and hands back a `302` to the platform CDN, so the client downloads directly. That is the cheapest and fastest delivery path there is — it just cannot combine two streams into one file.

---

## Which host

| Host | Free? | Verdict |
|---|---|---|
| **Render** | Yes | **Best free choice.** Docker, so ffmpeg works. Full mode. Watch the 5 GB/month egress. |
| **Koyeb** | Yes | Close second. Docker, 512 MB, one free instance. |
| **Google Cloud Run** | Yes (generous) | **Best overall.** Docker, 3600 s timeouts, scales to zero. |
| **Vercel** | Yes | Resolver mode only. Excellent at it. No YouTube video. |
| **PythonAnywhere** | Free: **no** | Free tier **cannot work at all** — see below. Paid ($5/mo) works well. |
| **Pella** | Yes | **Not viable.** 100 MB RAM is below what this needs to start. |
| **Fly.io** | **No longer** | New accounts get no free allowance; billed from the first machine. |
| **AWS App Runner** | No | Closed to new customers since 2026-03-31. Hard 120 s request cap. |
| **Your own VPS** | ~$4/mo | No limits, no cold starts, your own IP reputation. Best if you use this daily. |

### Where it shines

**Cloud Run and Render.** Both run the Docker image, so ffmpeg is present, nothing caps the response body, and every feature works. Pick Cloud Run if you want headroom, Render if you want the simplest free setup.

**Vercel, in resolver mode.** Its edge network and instant cold starts make metadata lookups and CDN redirects genuinely fast, and the work is pure JSON and 302s — exactly what serverless is good at. Use it as a resolver API in front of a client that downloads for itself.

### Where it does not

**Vercel for media transfer.** Function responses are capped at **4.5 MB**; anything larger fails with `FUNCTION_RESPONSE_PAYLOAD_TOO_LARGE`. There is no ffmpeg and the filesystem is read-only apart from a small `/tmp`. Vercel's own guidance is that functions are an API layer, not a media server. This is a platform design decision and no amount of configuration gets around it.

**PythonAnywhere free.** Free accounts reach the internet only through a proxy restricted to an **allowlist**, and the policy is that a site needs an official public API to be added. YouTube and Instagram are not on it and will not be. Every extraction fails at the network layer. The paid Hacker plan has unrestricted access and does work.

**Pella free.** 0.1 CPU and 100 MB RAM. Python plus Flask plus yt-dlp needs roughly 80–120 MB resident before serving a single request, so it will OOM on startup or shortly after. It is built for small Discord bots.

---

## Local Windows

### Development

```powershell
cd lite
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

winget install Gyan.FFmpeg       # then restart your shell
ffmpeg -version                  # confirm it is on PATH

$env:API_KEYS = "dev-local-key"
python app.py
```

Open <http://localhost:8080>.

### As a personal app

For your own machine, drop the key and bind to loopback only, so nothing on your network can reach it:

Create `start-clipfetch.bat`:

```bat
@echo off
cd /d "%~dp0lite"
set REQUIRE_API_KEY=false
set HOST=127.0.0.1
set PORT=8080
set ENABLE_UI=true
start "" http://127.0.0.1:8080
.venv\Scripts\python.exe app.py
```

`REQUIRE_API_KEY=false` is safe here *only* because it listens on `127.0.0.1`. The moment you expose it to your LAN or the internet, set a real key — otherwise anyone who can reach the port can spend your bandwidth and burn your IP's reputation with the platforms.

To run it without a console window, use `pythonw.exe`, or install it as a service with [NSSM](https://nssm.cc/).

---

## Render (free, full mode)

The blueprint is committed at the **repository root** as `render.yaml` — that is where Render looks by default. Note that its `dockerfilePath` and `dockerContext` are resolved relative to the repo root, not to any service directory, which is why they read `./lite/Dockerfile` and `./lite`.

1. Push this repo to GitHub.
2. In Render: **New +** → **Blueprint** → pick the repo. It reads `render.yaml`.
3. Render generates `API_KEYS` for you. Copy it from **Environment** after the first deploy.
4. Wait for the first build (5–10 minutes; it builds the ffmpeg-bearing image).

```powershell
.\.venv\Scripts\python.exe smoke_test.py --base https://clipfetch.onrender.com --key YOUR_KEY
```

**Free-tier realities.** None are faults in this app:

- **Spins down after 15 minutes idle**, and the next request pays a ~1 minute cold start. Your first download after a quiet spell will feel broken but is not.
- **5 GB egress per month** since 2026-04-23 (it was 100 GB). Relaying media is bandwidth-heavy: roughly 500 clips at 10 MB and you are out. Prefer `/api/v1/redirect`, which spends none of your egress.
- **750 instance-hours per workspace per month** against a ~730-hour month, so one always-on service eats nearly the whole budget. Do not add a keep-alive pinger and expect to stay free.
- **512 MB RAM, 0.1 CPU.** ffmpeg on a tenth of a core is slow; `render.yaml` sets `MAX_CONCURRENT=2` and a single worker to stay inside the memory limit.

---

## Koyeb (free, full mode)

One free instance per account, 512 MB, builds from a Dockerfile.

```powershell
# https://www.koyeb.com/docs/build-and-deploy/cli/installation
koyeb app init clipfetch `
  --git github.com/YOU/YOUR_REPO `
  --git-branch main `
  --git-workdir lite `
  --git-builder docker `
  --instance-type free `
  --regions was `
  --ports 8080:http `
  --routes /:8080 `
  --env SERVICE_MODE=full `
  --env REQUIRE_API_KEY=true `
  --env API_KEYS=@clipfetch-key `
  --env MAX_CONCURRENT=2 `
  --env WEB_CONCURRENCY=1 `
  --env THREADS=4 `
  --env MAX_FILESIZE_MB=200 `
  --env LOG_FORMAT=json
```

Create the secret first: `koyeb secret create clipfetch-key --value "$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"`.

Historically Koyeb kept free instances running rather than sleeping them, which is an advantage over Render, but they have announced scale-to-zero for idle services. Treat cold starts as possible.

---

## Vercel (free, resolver mode)

`vercel.json` and `api/index.py` are committed. `api/index.py` forces `SERVICE_MODE=resolver` in code rather than leaving it to an env var, because full mode cannot work here and a misconfiguration would produce confusing 500s instead of a clear message.

```powershell
npm i -g vercel
cd lite
vercel            # first run links the project
vercel --prod
```

In the Vercel dashboard, set:

- **Root Directory** → `lite` (so `requirements.txt` and `api/` are found)
- **Environment Variables** → `API_KEYS` = a long random value, `REQUIRE_API_KEY` = `true`

```powershell
.\.venv\Scripts\python.exe smoke_test.py --base https://your-app.vercel.app --key YOUR_KEY
```

The suite detects resolver mode and checks the right contract: `/redirect` returns a `302`, and `/download` returns `501` with an explanation.

**What you get:** fast metadata, fast CDN redirects, Instagram/TikTok/X/Facebook downloads, YouTube audio.
**What you do not:** YouTube video, mp3 conversion, any HLS-only source.

In-process rate limiting is disabled here because instances are ephemeral and per-instance counters mean little. Use Vercel's firewall if you need enforcement.

---

## PythonAnywhere

**The free tier cannot run this.** Free accounts reach the internet only via a proxy restricted to an allowlist of sites with official public APIs. YouTube and Instagram are not on it. Extraction fails at the network layer, so there is nothing to configure.

On the **paid Hacker plan** ($5/mo) internet access is unrestricted and ffmpeg is available, which makes it a decent always-on host with no cold starts:

1. **Web** → **Add a new web app** → **Manual configuration** → Python 3.11.
2. In a Bash console:
   ```bash
   git clone https://github.com/YOU/YOUR_REPO.git
   cd YOUR_REPO/lite
   mkvirtualenv --python=/usr/bin/python3.11 clipfetch
   pip install -r requirements.txt
   which ffmpeg     # confirm; if absent, set ENABLE_SERVER_DOWNLOAD=false
   ```
3. Point the **Virtualenv** field at `/home/YOU/.virtualenvs/clipfetch`.
4. Edit the WSGI file:
   ```python
   import os, sys
   path = "/home/YOU/YOUR_REPO/lite"
   if path not in sys.path:
       sys.path.insert(0, path)
   os.environ["API_KEYS"] = "your-long-random-key"
   os.environ["SERVICE_MODE"] = "full"
   os.environ["MAX_CONCURRENT"] = "2"
   from app import app as application
   ```
5. **Reload** the web app.

Note that PythonAnywhere runs one worker process; treat `MAX_CONCURRENT` as small.

---

## Pella

Not recommended. The free container is 0.1 CPU and 100 MB RAM, and this app needs roughly 80–120 MB resident before it serves anything. Expect an out-of-memory kill at startup or soon after. It is designed for small Discord bots, and a media downloader is the wrong shape of workload for it.

---

## Your own server

Best option if you use this daily: no cold starts, no bandwidth cap, and a residential or at least unshared IP, which the platforms challenge far less than a well-known cloud range.

A $4–6/month VPS (Hetzner, Vultr, DigitalOcean) with 1 vCPU and 1 GB RAM is plenty.

### Docker Compose with automatic TLS

```yaml
# docker-compose.prod.yml
services:
  clipfetch:
    build:
      context: ./lite
    restart: unless-stopped
    environment:
      API_KEYS: ${API_KEYS:?set API_KEYS in .env}
      REQUIRE_API_KEY: "true"
      SERVICE_MODE: full
      LOG_FORMAT: json
      MAX_CONCURRENT: "4"
      MAX_FILESIZE_MB: "500"
      MAX_DURATION_SEC: "600"
      SOFT_DEADLINE_SEC: "600"
      WEB_CONCURRENCY: "2"
      THREADS: "8"
    tmpfs:
      - /tmp:size=2g,mode=1777
    # No published ports: only Caddy can reach it.
    expose:
      - "8080"
    security_opt:
      - no-new-privileges:true

  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config

volumes:
  caddy_data:
  caddy_config:
```

```
# Caddyfile - Caddy obtains and renews a Let's Encrypt certificate itself.
clips.example.com {
    reverse_proxy clipfetch:8080 {
        # Muxing a long clip can take a while on a small VPS.
        transport http {
            response_header_timeout 10m
            read_timeout 10m
        }
    }
    encode zstd gzip
    header -Server
}
```

```bash
echo "API_KEYS=$(openssl rand -base64 32)" > .env
chmod 600 .env
docker compose -f docker-compose.prod.yml up -d --build
```

Point an A record at the server and Caddy handles the certificate on first request.

**Firewall.** Only 80 and 443 should be open. The app port is deliberately not published, so the container is reachable only through Caddy:

```bash
sudo ufw default deny incoming
sudo ufw allow 22,80,443/tcp
sudo ufw enable
```

### Keeping yt-dlp current

This matters more than anything else in day-to-day operation. Platforms change their players and extraction breaks, surfacing as `extraction_failed` (HTTP 502). Rebuild monthly:

```bash
# /etc/cron.monthly/clipfetch-update
cd /opt/clipfetch && docker compose -f docker-compose.prod.yml build --pull --no-cache && \
  docker compose -f docker-compose.prod.yml up -d && docker image prune -f
```

---

## After any deploy

```powershell
.\.venv\Scripts\python.exe smoke_test.py --base https://YOUR_HOST --key YOUR_KEY
```

The suite separates contract failures from network-dependent skips, so a platform bot-check does not read as a code defect. Then confirm:

- [ ] `GET /healthz` returns 200 and the health check path is configured on the host
- [ ] `GET /readyz` returns 200, not 503 (503 means auth is on with no keys set)
- [ ] `API_KEYS` is a long random value, stored as a secret, not in git
- [ ] `/api/v1/limits` shows the `service_mode` you expect
- [ ] `CORS_ORIGINS` lists your real frontend origins, or is empty for backend-only use
- [ ] `SSRF_GUARD` is `true` — it blocks cloud metadata endpoints and internal addresses
- [ ] A calendar reminder to rebuild monthly for yt-dlp

### If YouTube returns `blocked_by_platform`

Expected on any cloud host. Datacenter IP ranges get challenged far more than residential ones; it is a property of where the container runs, not a bug. In order of effectiveness:

1. `PROXY=http://user:pass@host:port` — a residential or mobile proxy. By far the biggest improvement.
2. `COOKIES_B64` — base64 of a Netscape `cookies.txt` from a logged-in session. Required for private or age-gated media regardless. These are live credentials: store them as a secret and use a throwaway account.
3. `IMPERSONATE=chrome` — `curl-cffi` ships in the image and makes the TLS fingerprint look like a browser.
4. `EXTRACTOR_ARGS={"youtube":{"player_client":["android","web_safari"]}}` — switching the player client often clears it outright.

A self-hosted server on a residential connection avoids most of this.
