# docker/api.Dockerfile
# Packages the FastAPI gateway (app/main.py) — the public face of the system.
#
# Responsibilities:
#   • Receive GitHub webhook events (HMAC-verified)
#   • Enqueue run_issue tasks onto the Redis/Celery broker
#   • Serve the read API for run status and step traces (backed by PostgreSQL)
#
# This image does NOT contain heavy ML deps (sentence-transformers, chromadb).
# It only needs FastAPI, its transport layer, the DB client, and the Celery
# client (to enqueue tasks — it never executes them).
#
# Build:
#   docker build -f docker/api.Dockerfile -t auto-swe-api:latest .
#
# Run (standalone, for local testing):
#   docker run --env-file .env -p 8000:8000 auto-swe-api:latest
#
# In production use docker-compose / Kubernetes to wire it to Redis & Postgres.

# ── Base ─────────────────────────────────────────────────────────────────────
# Pin to a specific digest in production for a reproducible supply chain.
FROM python:3.12-slim-bookworm

# ── System deps ───────────────────────────────────────────────────────────────
# We only need libpq for the psycopg2-binary wheel and curl for healthchecks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── App user (non-root) ───────────────────────────────────────────────────────
# Running as non-root is mandatory for hardened production deployments.
RUN groupadd --gid 1001 appgroup \
    && useradd --uid 1001 --gid appgroup --shell /bin/bash --create-home appuser

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
# Copy ONLY requirements first so Docker can cache this layer separately.
# The heavy ML deps (sentence-transformers, chromadb, tree-sitter) are NOT
# needed here — they live in the worker image only.
COPY requirements.txt .

# Install a trimmed set: FastAPI stack + Celery client + DB driver.
# We use --no-cache-dir to keep the image lean.
RUN pip install --no-cache-dir \
        fastapi>=0.115 \
        uvicorn[standard]>=0.30 \
        httpx \
        celery>=5.3 \
        redis>=5.0 \
        sqlalchemy>=2.0 \
        alembic>=1.13 \
        psycopg2-binary>=2.9 \
        python-dotenv \
        anthropic>=0.40 \
        google-genai>=1.0

# ── Application source ────────────────────────────────────────────────────────
# Copy the source packages the API needs. We deliberately exclude venv/,
# agent/retrieval.py heavy deps, and other worker-only code.
COPY app/       ./app/
COPY agent/     ./agent/
COPY workers/   ./workers/
COPY db/        ./db/
COPY alembic.ini .

# ── Ownership ─────────────────────────────────────────────────────────────────
RUN chown -R appuser:appgroup /app
USER appuser

# ── Port ──────────────────────────────────────────────────────────────────────
EXPOSE 8000

# ── Healthcheck ───────────────────────────────────────────────────────────────
# Docker and Kubernetes use this to know when the container is ready.
# /healthz is a lightweight endpoint in app/main.py.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

# ── Entrypoint ────────────────────────────────────────────────────────────────
# Run uvicorn with 2 worker processes. For more scale, increase --workers
# or use a Kubernetes HPA to spin up more replicas of this container.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--log-level", "info"]
