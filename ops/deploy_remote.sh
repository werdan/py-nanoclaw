#!/usr/bin/env bash
set -euo pipefail

# Run locally. Updates code on the remote via git, then builds and starts compose.
#
# Usage:
#   bash ops/deploy_remote.sh user@host [remote_app_dir]
#
# Git (default):
#   - If the remote dir has no repo: set NANOCLAW_GIT_REMOTE to clone once, or clone manually.
#   - Otherwise: git fetch + fast-forward pull on NANOCLAW_GIT_BRANCH (default: main).
#
# Legacy rsync (optional):
#   DEPLOY_SYNC=rsync bash ops/deploy_remote.sh user@host ...

if [ "${1:-}" = "" ]; then
  echo "Usage: bash ops/deploy_remote.sh user@host [remote_app_dir]"
  exit 1
fi

REMOTE_HOST="$1"
REMOTE_APP_DIR="${2:-/opt/nanoclaw}"

REMOTE_USER="${REMOTE_HOST%@*}"
if [ "${REMOTE_USER}" = "${REMOTE_HOST}" ]; then
  REMOTE_USER=""
fi

DEPLOY_SYNC="${DEPLOY_SYNC:-git}"
GIT_BRANCH="${NANOCLAW_GIT_BRANCH:-main}"
GIT_REMOTE="${NANOCLAW_GIT_REMOTE:-}"

echo "==> Ensuring remote app directory exists"
ssh "${REMOTE_HOST}" "mkdir -p '${REMOTE_APP_DIR}'"

if [ "${DEPLOY_SYNC}" = "rsync" ]; then
  echo "==> Syncing repository to remote (rsync)"
  rsync -az --delete \
    --exclude ".git" \
    --exclude ".venv" \
    --exclude "__pycache__" \
    --exclude ".pytest_cache" \
    ./ "${REMOTE_HOST}:${REMOTE_APP_DIR}/"
else
  echo "==> Updating code on remote (git, branch=${GIT_BRANCH})"
  ssh "${REMOTE_HOST}" bash -s <<REMOTE_SCRIPT
set -euo pipefail
APP_DIR='${REMOTE_APP_DIR}'
BRANCH='${GIT_BRANCH}'
REMOTE_URL='${GIT_REMOTE}'

if [ ! -d "\${APP_DIR}/.git" ]; then
  if [ -n "\${REMOTE_URL}" ]; then
    rm -rf "\${APP_DIR}"
    git clone --branch "\${BRANCH}" "\${REMOTE_URL}" "\${APP_DIR}"
  else
    echo "Remote \${APP_DIR} is not a git clone."
    echo "Either clone once on the server, e.g.:"
    echo "  sudo mkdir -p \$(dirname \"\${APP_DIR}\") && sudo git clone <your-repo-url> \"\${APP_DIR}\""
    echo "Or set NANOCLAW_GIT_REMOTE and rerun deploy."
    exit 1
  fi
else
  cd "\${APP_DIR}"
  git fetch origin
  git checkout "\${BRANCH}"
  git pull --ff-only origin "\${BRANCH}"
fi
REMOTE_SCRIPT
fi

echo "==> Bootstrapping remote host packages and Docker"
ssh "${REMOTE_HOST}" \
  "cd '${REMOTE_APP_DIR}' && sudo APP_DIR='${REMOTE_APP_DIR}' TARGET_USER='${REMOTE_USER}' bash ops/remote_setup.sh"

echo "==> Preparing env file"
ssh "${REMOTE_HOST}" "cd '${REMOTE_APP_DIR}' && \
  if [ ! -f .env ]; then \
    cp .env.example .env; \
    echo 'Created .env from .env.example. Fill it and rerun deploy.'; \
    exit 2; \
  fi"

echo "==> Building agent image (nanoclaw-agent)"
ssh "${REMOTE_HOST}" "cd '${REMOTE_APP_DIR}' && sudo docker build -f container/Dockerfile -t nanoclaw-agent ."

echo "==> Deploying compose (bot + OneCLI + docker-socket-proxy)"
ssh "${REMOTE_HOST}" "cd '${REMOTE_APP_DIR}' && sudo docker compose up -d --build"

echo "==> Current containers"
ssh "${REMOTE_HOST}" "cd '${REMOTE_APP_DIR}' && sudo docker compose ps"
