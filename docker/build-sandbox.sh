#!/usr/bin/env bash
# Build the hardened sandbox Docker image for the autonomous SWE agent.
# Usage: ./docker/build-sandbox.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

docker build \
  -f "$SCRIPT_DIR/sandbox.Dockerfile" \
  -t auto-swe-sandbox:latest \
  "$PROJECT_ROOT"

echo "✅ Sandbox image built: auto-swe-sandbox:latest"
echo "   Set SANDBOX_IMAGE=auto-swe-sandbox:latest in .env"
