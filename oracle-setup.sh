#!/bin/bash
set -euo pipefail

APP_USER="amazonbot"
APP_NAME="amazon-bot"
APP_DIR="/opt/${APP_NAME}"
PYTHON_BIN="python3"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
ENV_FILE="${APP_DIR}/.env"
VENV_DIR="${APP_DIR}/venv"
ENV_CREATED=0

log() {
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"
}

require_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "This script must be run as root: sudo ./oracle-setup.sh" >&2
    exit 1
  fi
}

install_packages() {
  log "Updating system and installing dependencies..."
  apt update
  apt install -y "${PYTHON_BIN}" python3-venv python3-pip git rsync ca-certificates curl iptables-persistent

  log "Opening firewall ports..."
  iptables -A INPUT -p tcp --dport 443 -j ACCEPT
  iptables -A INPUT -p tcp --dport 80 -j ACCEPT
  netfilter-persistent save 2>/dev/null || true
}

create_user() {
  if id "${APP_USER}" &>/dev/null; then
    log "User ${APP_USER} already exists."
  else
    log "Creating service user ${APP_USER}..."
    useradd -r -m -d "${APP_DIR}" -s /usr/sbin/nologin "${APP_USER}"
  fi
}

setup_app_dir() {
  log "Preparing app directory: ${APP_DIR}"
  mkdir -p "${APP_DIR}"
  mkdir -p "${APP_DIR}/data"
  chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
}

copy_app_files() {
  log "Copying application files into ${APP_DIR}..."

  if git -C . rev-parse --git-dir > /dev/null 2>&1; then
    log "Pulling latest code from git..."
    git -C . pull origin main || true
  fi

  rsync -av --delete \
    --exclude '.git' \
    --exclude 'venv' \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.env' \
    --exclude '*.db' \
    --exclude '*.sqlite' \
    --exclude '*.sqlite3' \
    --exclude '*.log' \
    --exclude 'data/' \
    --exclude '/data/' \
    --exclude 'ssh-key-*.key' \
    ./ "${APP_DIR}/"

  chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

  echo "DEPLOYED_AT=$(date -Iseconds)" > "${APP_DIR}/.deploy-meta"
  chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.deploy-meta"
}

create_venv() {
  if [[ ! -d "${VENV_DIR}" ]]; then
    log "Creating virtual environment..."
    sudo -u "${APP_USER}" "${PYTHON_BIN}" -m venv "${VENV_DIR}"
  else
    log "Virtual environment already exists."
  fi
}

install_requirements() {
  if [[ -f "${APP_DIR}/requirements.txt" ]]; then
    log "Installing Python dependencies..."
    sudo -u "${APP_USER}" bash -lc "
      source '${VENV_DIR}/bin/activate'
      python -m pip install --upgrade pip
      python -m pip install -r '${APP_DIR}/requirements.txt'
    "
  else
    log "No requirements.txt found, skipping dependency install."
  fi
}

install_playwright() {
  log "Installing Playwright dependencies and browsers..."
  sudo -u "${APP_USER}" bash -lc "
    source '${VENV_DIR}/bin/activate'
    python -m playwright install-deps chromium
    python -m playwright install chromium
  "
}

create_env_file() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    ENV_CREATED=1
    log "Creating .env file at ${ENV_FILE}..."

    cat > "${ENV_FILE}" <<EOF
# Environment variables for ${APP_NAME}

BOT_TOKEN=
CHAT_ID=

AMAZON_EMAIL=
AMAZON_PIN=
AMAZON_COOKIES=

DECODO_USER=
DECODO_PASS=
DECODO_HOST=gb.decodo.com
DECODO_PORT=30004
PROXY_POOL=

DATA_DIR=${APP_DIR}/data
DEBUG=0
MAX_ACCOUNTS=5
COOKIE_FRESH_HOURS=12
ENABLE_FULL_SUBMIT=false
EOF

    chown "${APP_USER}:${APP_USER}" "${ENV_FILE}"
    chmod 600 "${ENV_FILE}"
  else
    log ".env file already exists."
  fi
}

create_systemd_service() {
  log "Creating/updating systemd service: ${SERVICE_FILE}"

  cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Amazon Job Bot
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStartPre=/usr/bin/test -s ${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python ${APP_DIR}/main.py
Restart=always
RestartSec=10
KillSignal=SIGINT
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal
ProtectSystem=full
ProtectHome=true
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${APP_NAME}.service"
}

clean_old_logs() {
  log "Cleaning old journal logs..."
  journalctl --vacuum-time=7d || true
}

start_or_wait() {
  if [[ "${ENV_CREATED}" -eq 1 ]]; then
    log "Service not started because .env was just created."
    echo
    echo "Edit your secrets first:"
    echo "  sudo nano ${ENV_FILE}"
    echo
    echo "Then start the bot:"
    echo "  sudo systemctl restart ${APP_NAME}.service"
    echo
    echo "View logs:"
    echo "  sudo journalctl -u ${APP_NAME}.service -f"
    return
  fi

  log "Restarting ${APP_NAME} service..."
  systemctl restart "${APP_NAME}.service"
  sleep 3

  BOT_TOKEN=$(grep -E '^BOT_TOKEN=' "${ENV_FILE}" | cut -d= -f2- || true)
  CHAT_ID=$(grep -E '^CHAT_ID=' "${ENV_FILE}" | cut -d= -f2- || true)

  if [[ -n "${BOT_TOKEN}" && -n "${CHAT_ID}" ]]; then
    curl -s "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
      -d chat_id="${CHAT_ID}" \
      -d text="✅ Amazon Bot deployed and running on Oracle London" > /dev/null || true
    log "Telegram health check sent."
  else
    log "Telegram health check skipped (BOT_TOKEN or CHAT_ID missing)."
  fi

  systemctl status "${APP_NAME}.service" --no-pager || true
}

print_commands() {
  echo
  echo "✅ Setup complete."
  echo
  echo "Useful commands:"
  echo "  Edit env: sudo nano ${ENV_FILE}"
  echo "  Start:    sudo systemctl start ${APP_NAME}.service"
  echo "  Stop:     sudo systemctl stop ${APP_NAME}.service"
  echo "  Restart:  sudo systemctl restart ${APP_NAME}.service"
  echo "  Status:   sudo systemctl status ${APP_NAME}.service"
  echo "  Logs:     sudo journalctl -u ${APP_NAME}.service -f"
  echo
  echo "To redeploy after changing files:"
  echo "  cd <your-repo-folder>"
  echo "  sudo ./oracle-setup.sh"
  echo
}

main() {
  require_root
  install_packages
  create_user
  setup_app_dir
  copy_app_files
  create_venv
  install_requirements
  install_playwright
  create_env_file
  create_systemd_service
  clean_old_logs
  start_or_wait
  print_commands
}

main "$@"
