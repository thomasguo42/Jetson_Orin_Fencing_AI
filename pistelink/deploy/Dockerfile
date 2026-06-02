# PisteLink backend image — FastAPI + the built Vue SPA. The AI process is NOT
# in this image (it runs as a separate host process; see deploy/README.md).
# Build context is the repo root:  docker build -f deploy/Dockerfile -t pistelink/backend .
# syntax=docker/dockerfile:1
#
# npm / pip / apt default to CHINA mirrors (this is a backup deployment for a
# domestic network). For an international build, override the args, e.g.:
#   --build-arg NPM_REGISTRY=https://registry.npmjs.org \
#   --build-arg PIP_INDEX_URL=https://pypi.org/simple \
#   --build-arg APT_MIRROR=deb.debian.org
# Base images (node/python from Docker Hub) are NOT mirrored here — configure the
# Docker daemon's registry-mirrors for those (see deploy/README.md「备选：Docker」).

# ── Stage 1: build the Vue SPA ────────────────────────────────────────────
FROM node:20-bookworm-slim AS frontend
ARG NPM_REGISTRY=https://registry.npmmirror.com
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --registry="${NPM_REGISTRY}"
COPY frontend/ ./
RUN npm run build          # → /build/frontend/dist

# ── Stage 2: Python runtime ───────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS runtime
ARG APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# mpg123: audio playback (backend shells out to it). curl: HEALTHCHECK.
# Swap the Debian mirror first (handles both deb822 .sources and classic
# sources.list; either may be absent depending on the base image).
RUN if [ -n "${APT_MIRROR}" ]; then \
        sed -i "s/deb.debian.org/${APT_MIRROR}/g; s/security.debian.org/${APT_MIRROR}/g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
        sed -i "s/deb.debian.org/${APT_MIRROR}/g; s/security.debian.org/${APT_MIRROR}/g" \
            /etc/apt/sources.list 2>/dev/null || true; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends mpg123 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt

COPY backend/ ./backend/
COPY sound/ ./sound/
COPY --from=frontend /build/frontend/dist ./frontend/dist

ENV PYTHONUNBUFFERED=1 \
    PISTELINK_CONFIG=/etc/pistelink/config.toml \
    PISTELINK_FRONTEND_DIR=/app/frontend/dist \
    PISTELINK_SOUND_DIR=/app/sound

# network_mode: host means this is informational only.
EXPOSE 8080

# Port mirrors [http].port; the backend binds 127.0.0.1 by default (see config).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

# Container runs as root (no USER): single-device/single-user, so it can reach
# the AI's 0600 nvidia socket and all host-mounted dirs without UID matching.
CMD ["python", "-m", "backend.main"]
