# Clip Download API - Lite
#
# One image, several hosts: AWS App Runner, Google Cloud Run, ECS Fargate,
# Lightsail Containers, Fly.io, or plain `docker run`. The server reads $PORT at
# startup, which is what App Runner and Cloud Run inject.
#
# IMPORTANT: build for linux/amd64. App Runner and Cloud Run reject arm64
# images, and an Apple Silicon or Windows-on-ARM machine will quietly produce
# one. Pass the platform on the command line rather than pinning it in FROM,
# which would block multi-platform builds:
#
#   docker build --platform linux/amd64 -t clip-api-lite .
#
# The scripts in deploy/ already pass it.

FROM python:3.12-slim-bookworm

# ffmpeg powers audio conversion and merging of split video/audio streams.
# Drop it (and set ENABLE_SERVER_DOWNLOAD=false) for a ~120 MB smaller image if
# you only ever need direct and proxied single-file downloads.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so code edits do not invalidate the pip layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ ./core/
COPY templates/ ./templates/
COPY static/ ./static/
COPY app.py wsgi.py openapi.yaml ./

# Run unprivileged. /tmp stays writable for the ffmpeg scratch path.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

ENV PORT=8080 \
    TMP_DIR=/tmp \
    WEB_CONCURRENCY=2 \
    THREADS=8
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8080}/healthz" || exit 1

# --timeout 0 disables gunicorn's worker watchdog. It must be off: the watchdog
# measures time since the worker last touched the event loop and would kill a
# worker in the middle of a long streaming response, taking every other
# in-flight request with it. Request duration is bounded in the application by
# SOFT_DEADLINE_SEC instead.
#
# gthread workers suit this workload: requests are I/O-bound on the upstream
# CDN, so threads are cheap and memory stays low.
CMD ["sh", "-c", "exec gunicorn \
    --bind 0.0.0.0:${PORT:-8080} \
    --workers ${WEB_CONCURRENCY:-2} \
    --worker-class gthread \
    --threads ${THREADS:-8} \
    --timeout 0 \
    --graceful-timeout 30 \
    --keep-alive 65 \
    --access-logfile - \
    --error-logfile - \
    --log-level ${GUNICORN_LOG_LEVEL:-info} \
    wsgi:app"]
