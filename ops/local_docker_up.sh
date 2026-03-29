#!/usr/bin/env bash
set -euo pipefail

# Run the stack locally with Docker Compose (Docker Desktop or Linux Docker).
#
# Usage:
#   bash ops/local_docker_up.sh
#
# Prerequisites:
#   - .env in repo root (copy from .env.example and fill secrets)
#   - COMPOSE_PROJECT_NAME=nanoclaw (default bridge nanoclaw_default; agents use nanoclaw_agent)

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "Create .env from .env.example and fill in secrets first."
  exit 1
fi

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-nanoclaw}"
mkdir -p runtime/sessions runtime/media

echo "==> Building agent image"
docker build -f container/Dockerfile -t nanoclaw-agent .

echo "==> Starting compose (project: ${COMPOSE_PROJECT_NAME})"
docker compose up -d --build

echo "==> Status"
docker compose ps

echo ""
echo "Logs: docker compose logs -f bot"
echo "       docker compose logs -f onecli"
