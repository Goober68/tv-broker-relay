# ── Frontend build stage ───────────────────────────────────────────────────────
FROM node:20-alpine AS frontend-builder
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install && chmod -R +x node_modules/.bin
COPY frontend/ ./
# RUN npm run build
RUN node node_modules/vite/bin/vite.js build --outDir /frontend/dist
# Output lands in /frontend/dist (vite.config.js outDir: 'dist')

# ── Python build stage ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /install /usr/local
COPY . .
# Copy built frontend assets into app/static
COPY --from=frontend-builder /frontend/dist ./app/static
RUN useradd -m -u 1001 relay && chown -R relay:relay /app
USER relay

# SERVICE arg selects which entry point to run:
#   "monolith" (default) — original all-in-one app
#   "relay"              — webhook ingestion only
#   "dashboard"          — user-facing API + SSE + frontend
#   "worker"             — background tasks only
ARG SERVICE=monolith
ENV SERVICE=${SERVICE}

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/health || exit 1

EXPOSE ${PORT:-8000}

# Use shell form so $SERVICE and $PORT are expanded at runtime
CMD uvicorn app.main_${SERVICE}:app --host 0.0.0.0 --port ${PORT:-8000} \
    --workers 1 --log-level info --access-log
