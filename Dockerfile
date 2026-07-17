# MediaTrack — production image
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000 \
    ENVIRONMENT=production \
    PHOTOS_DIR=/data/photos

# postgresql-client provides pg_dump, used by GET /api/backup.
# curl is used by the container HEALTHCHECK.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so code edits don't bust the layer cache.
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/

# Uploaded photos live on a mounted volume, never inside the image.
RUN mkdir -p /data/photos \
 && adduser --disabled-password --gecos "" --uid 10001 mediatrack \
 && chown -R mediatrack:mediatrack /app /data
USER mediatrack

WORKDIR /app/backend
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS "http://localhost:${PORT}/api/health" || exit 1

# Single worker: uvicorn + this app is I/O bound and the free/starter Render
# instances are single-CPU. Scale with WEB_CONCURRENCY if you move up a tier.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY:-1} --proxy-headers --forwarded-allow-ips='*'"]
