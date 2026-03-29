#!/usr/bin/env bash
set -euo pipefail

# Run on remote host (Ubuntu/Debian) as root/sudo.
# Installs Docker + compose plugin and prepares runtime directories.

APP_DIR="${APP_DIR:-/opt/nanoclaw}"
TARGET_USER="${TARGET_USER:-${SUDO_USER:-}}"

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y ca-certificates curl gnupg lsb-release rsync git

install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
fi

ARCH="$(dpkg --print-architecture)"
CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME}")"
cat >/etc/apt/sources.list.d/docker.list <<EOF
deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${CODENAME} stable
EOF

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

systemctl enable docker
systemctl start docker

mkdir -p "${APP_DIR}/runtime/sessions" "${APP_DIR}/runtime/media"

if [ -n "${TARGET_USER}" ] && id "${TARGET_USER}" >/dev/null 2>&1; then
  usermod -aG docker "${TARGET_USER}" || true
  chown -R "${TARGET_USER}:${TARGET_USER}" "${APP_DIR}" || true
fi

echo "Remote setup complete."
