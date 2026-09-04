# StockBrain application image.
#
# One container runs the API, the job workers, the scheduler and (in later
# phases) the Alpaca WebSocket and Telegram long-polling tasks. The compiled
# React bundle is baked in and served by FastAPI from the same origin, so no
# second web server and no CORS relaxation are needed.
#
# Build from the repository root:
#   docker build -f backend/Dockerfile -t stockbrain:local .

# ---------------------------------------------------------------------------
# Stage 1: build the frontend
# ---------------------------------------------------------------------------
FROM node:22-alpine AS frontend

WORKDIR /build

# package*.json first so dependency installation is cached independently of
# source changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: python runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# curl is used by nothing at runtime; the healthcheck uses python's urllib so we
# keep the image free of extra tooling.
RUN groupadd --system --gid 10001 stockbrain \
    && useradd --system --uid 10001 --gid stockbrain --create-home stockbrain \
    && mkdir -p /data && chown stockbrain:stockbrain /data

WORKDIR /app

# Pinned, fully resolved dependency set. Installed before the application source
# so a code change does not invalidate the dependency layer.
COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY backend/pyproject.toml ./pyproject.toml
COPY backend/alembic.ini ./alembic.ini
COPY backend/alembic ./alembic
COPY backend/stockbrain ./stockbrain

# The SPA is served from stockbrain/../static (see stockbrain.main.FRONTEND_DIST).
COPY --from=frontend /build/dist ./static

COPY backend/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER stockbrain

EXPOSE 8080
VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["serve"]
