# docker/worker.Dockerfile
# Packages the Celery worker — the agent's "body" that does all heavy lifting.
#
# Responsibilities:
#   • Execute run_issue tasks dequeued from Redis
#   • Clone repositories, run the ReAct loop, call the LLM provider
#   • Index codebases with tree-sitter + sentence-transformers + ChromaDB
#   • Manage the ephemeral sandbox containers (docker-in-docker socket mount)
#   • Commit changes, push branches, and open pull requests via GitHub API
#
# This image IS heavy — it includes all ML dependencies. Keep the api image
# lean by never adding these deps there.
#
# Build:
#   docker build -f docker/worker.Dockerfile -t auto-swe-worker:latest .
#
# Run (standalone, for local testing):
#   docker run --env-file .env \
#       -v /var/run/docker.sock:/var/run/docker.sock \
#       -v /var/agent/workspaces:/var/agent/workspaces \
#       auto-swe-worker:latest
#
# NOTE: The Docker socket is mounted so the worker can spin up + tear down the
# ephemeral sandbox containers (use_sandbox=True). In production, consider a
# dedicated Docker daemon socket with tighter ACLs.

# ── Base ──────────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm

# ── System deps ───────────────────────────────────────────────────────────────
# git      — the agent shells out to git for clone/branch/commit/push
# docker   — CLI needed to manage the sandbox containers (via mounted socket)
# build-essential / libffi-dev — needed to compile some ML dep C extensions
# curl     — healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        curl \
        build-essential \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Install the Docker CLI (only the client — daemon runs on the host).
# We fetch the static binary to avoid adding the full Docker apt repo.
RUN curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-27.3.1.tgz \
    | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
    && chmod +x /usr/local/bin/docker

# ── App user (non-root) ───────────────────────────────────────────────────────
# The worker needs to be in the 'docker' group so it can talk to the
# mounted Docker socket without running as root.
RUN groupadd --gid 1001 appgroup \
    && useradd --uid 1001 --gid appgroup --groups docker \
       --shell /bin/bash --create-home appuser 2>/dev/null || \
    useradd --uid 1001 --gid appgroup \
       --shell /bin/bash --create-home appuser

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
# Copy requirements first for Docker layer caching.
# The ML deps (sentence-transformers, chromadb, tree-sitter) are large and
# slow to install — caching this layer saves minutes on every rebuild.
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the sentence-transformers embedding model weights so the worker
# never has to hit Hugging Face on first use in production (air-gapped or
# cold-start scenarios). The model is baked into the image layer.
# This adds ~90 MB but eliminates the "downloading model on first task" delay.
RUN python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')" \
    || echo "WARNING: could not pre-download model (will download at runtime)"

# ── Application source ────────────────────────────────────────────────────────
COPY agent/     ./agent/
COPY workers/   ./workers/
COPY db/        ./db/
COPY app/       ./app/
COPY alembic.ini .

# ── Workspace root ────────────────────────────────────────────────────────────
# The agent clones repos here. This directory is typically bind-mounted from
# the host so workspaces survive container restarts during long runs.
RUN mkdir -p /var/agent/workspaces \
    && chown appuser:appgroup /var/agent/workspaces

# ── Git identity ──────────────────────────────────────────────────────────────
# Set a safe global git identity so `git commit` works out of the box.
# agent/github.py also sets a local per-repo identity, but this is the fallback.
RUN git config --global user.email "auto-swe-agent@users.noreply.github.com" \
    && git config --global user.name "auto-swe-agent" \
    && git config --global --add safe.directory '*'

# ── Ownership ─────────────────────────────────────────────────────────────────
RUN chown -R appuser:appgroup /app
USER appuser

# ── Healthcheck ───────────────────────────────────────────────────────────────
# Check that the Celery worker is alive by running `celery inspect ping`.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD celery -A workers inspect ping --destination celery@$$HOSTNAME \
        --timeout 5 2>/dev/null | grep -q "pong" || exit 1

# ── Entrypoint ────────────────────────────────────────────────────────────────
# --concurrency=2: run 2 parallel tasks per container. Each task is a full
#   clone+index+ReAct run, so 2 is a sensible default — increase via env var
#   CELERY_CONCURRENCY or by adding more worker container replicas.
# --max-tasks-per-child=10: recycle the worker process every 10 tasks to
#   prevent slow memory leaks from the ML models accumulating over time.
CMD ["celery", "-A", "workers", "worker", \
     "--loglevel=info", \
     "--concurrency=2", \
     "--max-tasks-per-child=10"]
