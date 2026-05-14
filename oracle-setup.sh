#!/bin/bash
set -euo pipefail

APP_NAME="amazon-bot"
APP_USER="amazonbot"

BASE_DIR="/opt/${APP_NAME}"
RELEASES_DIR="${BASE_DIR}/releases"
CURRENT_LINK="${BASE_DIR}/current"
SHARED_DIR="${BASE_DIR}/shared"

ENV_FILE="${BASE_DIR}/.env"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"

PYTHON_BIN="python3"

TIMESTAMP=$(date +%Y-%m-%d_%H-%M-%S)
NEW_RELEASE="${RELEASES_DIR}/${TIMESTAMP}"
VENV_DIR="${NEW_RELEASE}/venv"

log() { echo "[$(date +'%F %T')] $*"; }
fail() { echo "❌ $*" >&2; exit 1; }

require_root() {
  [[ "$(id -u)" -eq 0 ]] || fail "Run as root"
}

install_packages() {
  apt update
  apt install -y python3 python3-venv python3-pip rsync curl git
}

ensure_user() {
  id "${APP_USER}" &>/dev/null || \
  useradd -r -m -d "${BASE_DIR}" -s /usr/sbin/nologin "${APP_USER}"
}

ensure_dirs() {
  mkdir -p "${RELEASES_DIR}" "${SHARED_DIR}/data"
  chown -R "${APP_USER}:${APP_USER}" "${BASE_DIR}"
}

ensure_env() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    cat > "${ENV_FILE}" <<EOF
BOT_TOKEN=
CHAT_ID=
DATA_DIR=${SHARED_DIR}/data
EOF
    chmod 600 "${ENV_FILE}"
    chown "${APP_USER}:${APP_USER}" "${ENV_FILE}"
    fail "Fill .env then rerun"
  fi
}

copy_code() {
  cd /opt/amazon-bot-src
  git fetch origin
  git reset --hard origin/main

  mkdir -p "${NEW_RELEASE}"

  rsync -a --delete \
    --exclude '.git' \
    --exclude 'venv' \
    --exclude '__pycache__' \
    ./ "${NEW_RELEASE}/"

  git rev-parse HEAD > "${NEW_RELEASE}/.commit"

  chown -R "${APP_USER}:${APP_USER}" "${NEW_RELEASE}"
}

setup_venv() {
  sudo -u "${APP_USER}" python3 -m venv "${VENV_DIR}"

  sudo -u "${APP_USER}" bash -lc "
    source '${VENV_DIR}/bin/activate'
    pip install --upgrade pip
    pip install --no-cache-dir -r '${NEW_RELEASE}/requirements.txt'
  "
}

install_playwright() {
  if [[ ! -f "${BASE_DIR}/.playwright-installed" ]]; then
    sudo -u "${APP_USER}" bash -lc "
      source '${VENV_DIR}/bin/activate'
      python -m playwright install chromium
    "
    touch "${BASE_DIR}/.playwright-installed"
  fi
}

ensure_service() {
  cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Amazon Bot
After=network.target

[Service]
User=${APP_USER}
WorkingDirectory=${CURRENT_LINK}
EnvironmentFile=${ENV_FILE}

ExecStartPre=/usr/bin/test -f ${CURRENT_LINK}/main.py

ExecStart=${CURRENT_LINK}/venv/bin/python ${CURRENT_LINK}/main.py

Restart=always
RestartSec=5
LimitNOFILE=65535

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=${BASE_DIR}

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${APP_NAME}"
}

switch_release() {
  ln -sfn "${NEW_RELEASE}" "${CURRENT_LINK}"
  chown -h "${APP_USER}:${APP_USER}" "${CURRENT_LINK}"
}

restart_service() {
  systemctl restart "${APP_NAME}"
  sleep 2
}

cleanup() {
  find "${RELEASES_DIR}" -mindepth 1 -maxdepth 1 -type d \
    | sort -r | tail -n +4 | xargs -r rm -rf
}

main() {
  require_root
  install_packages
  ensure_user
  ensure_dirs
  ensure_env

  copy_code
  setup_venv
  install_playwright

  ensure_service
  switch_release
  restart_service
  cleanup

  log "✅ Deploy complete"
}

main "$@"
