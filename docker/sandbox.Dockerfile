# Sandbox execution image — the only place untrusted repo code runs.
#
# The container runs with --network none, so nothing can be pip-installed at
# run time (plan DECISION D9: pre-bake deps). Bake pytest and any common test
# dependencies HERE, then rebuild. For a specific target repo, extend this image
# (or a per-repo variant) with that repo's requirements.
#
# Build:
#   docker build -f docker/sandbox.Dockerfile -t auto-swe-sandbox:latest .
# Use:
#   SANDBOX_IMAGE=auto-swe-sandbox:latest python -m agent.sandbox
#   SANDBOX_IMAGE=auto-swe-sandbox:latest python -m agent.loop "..." --workspace <repo> --sandbox
#
# PRODUCTION: pin the base by digest (FROM python:3.12-slim-bookworm@sha256:...)
# so the supply chain is reproducible (plan §9). Tag pinning is used here for
# developer convenience.
FROM python:3.12-slim-bookworm

# coreutils provides `timeout`, which the sandbox uses for an in-container
# wall-clock kill (leaving the container alive for reuse). It is already present
# in the slim image; this line documents the dependency and is a no-op if so.
RUN apt-get update \
    && apt-get install -y --no-install-recommends coreutils \
    && rm -rf /var/lib/apt/lists/*

# Test tooling available offline inside the sandbox.
RUN pip install --no-cache-dir pytest

# Hardened defaults: non-root, writable scratch only under /tmp and the mounted
# /work. `docker run` in agent/sandbox.py also enforces --user/--read-only/etc,
# but setting a non-root default here is defense in depth.
USER 65534:65534
WORKDIR /work

# The runtime keeps the container alive; agent/sandbox.py dispatches work via
# `docker exec`. Overridden by `sleep infinity` at run time regardless.
CMD ["python", "--version"]
