# Clip Downloader

Download video and audio from YouTube, Instagram, TikTok, X, Facebook and the other ~1800 sites [yt-dlp](https://github.com/yt-dlp/yt-dlp) supports.

Two builds, because the constraints pull in opposite directions. A service that scales to zero on a managed host has to be stateless and finish inside a request timeout. A tool that pulls a 4K two-hour video needs a job queue and a disk. Trying to be both produces something that is bad at each.

| | [**Lite**](lite/) | **Pro** |
|---|---|---|
| Shape | Web app + HTTP API | Local app / self-hosted server |
| Runs on | Cloud Run, Render, Koyeb, Vercel, Docker | Your machine or your own server |
| Built for | Reels, Shorts, TikToks, clips up to ~5 min | Anything: long video, playlists, channels, 4K |
| Quality | Up to 1080p, size-capped | Up to 8K, full bitrate, original audio |
| Concurrency | Synchronous, one clip per call | Job queue, parallel downloads, resume |
| State | None | SQLite job store, media library on disk |
| Status | **Built and verified** | Not built yet |

## Lite

A minimal download page backed by a JSON API. Paste a link, press Download, get a file. No database, no queue, no persistent disk, so a managed host can scale it to zero and back.

```powershell
cd lite
docker compose up --build
# then open http://localhost:8080
```

Where to host it, with the limits that actually matter: **[lite/DEPLOYER.md](lite/DEPLOYER.md)**.
API reference, configuration and client libraries: [lite/README.md](lite/README.md).

### Where it runs

| Host | Free? | Verdict |
|---|---|---|
| Google Cloud Run | Yes | **Best overall.** Docker, 3600 s timeouts, scales to zero. |
| Render | Yes | **Best free option.** Blueprint committed. Watch the 5 GB/month egress. |
| Koyeb | Yes | One free 512 MB instance. |
| Vercel | Yes | Resolver mode only — great at it, but no YouTube video. |
| Your own VPS | ~$4/mo | No limits, no cold starts, better IP reputation. |
| PythonAnywhere free | Free: no | **Cannot work.** Outbound allowlist blocks the platforms. Paid works. |
| Pella free | Yes | **Not viable.** 100 MB RAM. |
| Fly.io | No longer | New accounts get no free allowance. |
| AWS App Runner | No | Closed to new customers since 2026-03-31. Hard 120 s request cap. |

### Three things worth knowing before you deploy

**ffmpeg is required for YouTube.** YouTube has retired combined video+audio formats. Measured against a real Short: 29 video-only streams, 10 audio-only, zero combined. So YouTube video always needs muxing, which splits hosts in two. `SERVICE_MODE=full` (Docker, a VPS, your own machine) does everything. `SERVICE_MODE=resolver` runs anywhere Python runs and returns CDN redirects instead of media — fast and cheap, but it cannot combine two streams, so YouTube video is out. Instagram, TikTok, X and Facebook serve single files and work in both modes.

**Vercel cannot transfer media.** Function responses are capped at 4.5 MB and there is no ffmpeg, so it is resolver-only by design, not by oversight. That is still genuinely useful: metadata and 302s are exactly what serverless is good at.

**Expect bot checks on cloud IPs.** Datacenter ranges get challenged far more than residential ones, so `blocked_by_platform` on YouTube is normal from any cloud host and is not a defect in this code. The levers, in order of effectiveness: a residential proxy (`PROXY`), session cookies (`COOKIES_B64`), TLS impersonation (`IMPERSONATE=chrome`), and player-client overrides (`EXTRACTOR_ARGS`). A self-hosted box on a home connection sidesteps most of it.

## Pro

Not built yet. Intended scope: a local web app plus CLI, a SQLite-backed job queue with parallel workers, resume and cancel, whole playlists and channels, quality selection up to 8K, audio extraction at full bitrate, subtitles, embedded metadata and chapters, a browsable media library, and cookie import from your installed browser.

## Legal

You are responsible for what you download. Respect copyright and each platform's terms of service. Intended for media you own, media you have permission to use, or content licensed for reuse.
