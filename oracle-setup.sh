#!/usr/bin/env bash
set -euo pipefail

APP_NAME="amazon-bot"
APP_USER="amazonbot"

BASE_DIR="/opt/${APP_NAME}"
SRC_DIR="/opt/amazon-bot-src"
RELEASES_DIR="${BASE_DIR}/releases"
CURRENT_LINK="${BASE_DIR}/current"
SHARED_DIR="${BASE_DIR}/shared"
ENV_FILE="${BASE_DIR}/.env"
LOCK_FILE="${BASE_DIR}/deploy.lock"
LOG_FILE="${BASE_DIR}/deploy.log"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"

HEALTH_URL="${HEALTH_URL:-}"
KEEP_RELEASES=5

TIMESTAMP="$(date +%Y-%m-%d_%H-%M-%S)"
NEW_RELEASE="${RELEASES_DIR}/${TIMESTAMP}"
VENV_DIR="${NEW_RELEASE}/venv"
PREVIOUS_RELEASE=""

log() {
  echo "[$(date +'%F %T')] $*" | tee -a "${LOG_FILE:-/tmp/deploy.log}"
}

fail() {
  log "ERROR: $*"
  exit 1
}

require_root() {
  [[ "$(id -u)" -eq 0 ]] || fail "Run as root"
}

install_packages() {
  apt-get update
  apt-get install -y python3 python3-venv python3-pip rsync curl git ca-certificates
}

ensure_user() {
  id "${APP_USER}" >/dev/null 2>&1 || \
    useradd -r -m -d "${BASE_DIR}" -s /usr/sbin/nologin "${APP_USER}"
}

ensure_dirs() {
  mkdir -p "${BASE_DIR}" "${RELEASES_DIR}" "${SHARED_DIR}/data"
  touch "${LOG_FILE}"
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
    fail "Created ${ENV_FILE}. Fill it in, then rerun."
  fi
}

diagnostics() {
  log "Collecting diagnostics"
  systemctl status "${APP_NAME}" --no-pager || true
  journalctl -u "${APP_NAME}" -n 120 --no-pager || true
  df -h || true
  free -m || true
}

verify_service() {
  if ! systemctl is-active --quiet "${APP_NAME}"; then
    log "systemd reports ${APP_NAME} is not active"
    return 1
  fi

  if [[ -n "${HEALTH_URL}" ]]; then
    for i in 1 2 3 4 5; do
      if curl -fsS --max-time 10 "${HEALTH_URL}" >/dev/null; then
        log "Health check passed"
        return 0
      fi
      log "Health check attempt ${i} failed"
      sleep 3
    done

    log "Health check failed: ${HEALTH_URL}"
    return 1
  fi

  log "No HEALTH_URL set; systemd check passed"
  return 0
}

rollback() {
  log "ROLLBACK START"

  if [[ -n "${PREVIOUS_RELEASE}" && -d "${PREVIOUS_RELEASE}" ]]; then
    ln -sfn "${PREVIOUS_RELEASE}" "${BASE_DIR}/current_tmp"
    mv -Tf "${BASE_DIR}/current_tmp" "${CURRENT_LINK}"
    chown -h "${APP_USER}:${APP_USER}" "${CURRENT_LINK}"

    systemctl restart "${APP_NAME}" || true
    sleep 5

    if verify_service; then
      log "ROLLBACK SUCCESS: ${PREVIOUS_RELEASE}"
    else
      log "ROLLBACK FAILED VERIFICATION"
      diagnostics
    fi
  else
    log "No previous release available"
    diagnostics
  fi

  exit 1
}

ensure_source_repo() {
  [[ -d "${SRC_DIR}/.git" ]] || fail "Source repo missing: ${SRC_DIR}"
}

prepare_release() {
  cd "${SRC_DIR}"

  git fetch origin main
  COMMIT="$(git rev-parse origin/main)"

  log "Deploying commit: ${COMMIT}"

  mkdir -p "${NEW_RELEASE}"

  git archive "${COMMIT}" | tar -x -C "${NEW_RELEASE}"

  echo "${COMMIT}" > "${NEW_RELEASE}/.commit"

  [[ -f "${NEW_RELEASE}/requirements.txt" ]] || fail "requirements.txt missing"
  [[ -f "${NEW_RELEASE}/main.py" ]] || fail "main.py missing"

  ln -sfn "${ENV_FILE}" "${NEW_RELEASE}/.env"

  chown -R "${APP_USER}:${APP_USER}" "${NEW_RELEASE}"
}

setup_venv() {
  log "Creating virtualenv"
  sudo -u "${APP_USER}" python3 -m venv "${VENV_DIR}"

  log "Installing dependencies"
  sudo -u "${APP_USER}" "${VENV_DIR}/bin/python" -m pip install --upgrade pip
  sudo -u "${APP_USER}" "${VENV_DIR}/bin/pip" install --no-cache-dir -r "${NEW_RELEASE}/requirements.txt"

  log "Compile check"
  sudo -u "${APP_USER}" "${VENV_DIR}/bin/python" -m compileall "${NEW_RELEASE}" >/dev/null
}

install_playwright_browsers() {
  if sudo -u "${APP_USER}" "${VENV_DIR}/bin/python" -c "import playwright" >/dev/null 2>&1; then
    log "Installing Playwright Chromium"
    sudo -u "${APP_USER}" env PLAYWRIGHT_BROWSERS_PATH="${SHARED_DIR}/playwright" \
      "${VENV_DIR}/bin/python" -m playwright install chromium
  else
    log "Playwright not installed; skipping browser install"
  fi
}

ensure_service() {
  cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Amazon Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${CURRENT_LINK}
EnvironmentFile=${ENV_FILE}
Environment=PLAYWRIGHT_BROWSERS_PATH=${SHARED_DIR}/playwright

ExecStartPre=/usr/bin/test -f ${CURRENT_LINK}/main.py
ExecStart=${CURRENT_LINK}/venv/bin/python ${CURRENT_LINK}/main.py

Restart=always
RestartSec=5
TimeoutStartSec=60
TimeoutStopSec=30
KillSignal=SIGTERM
LimitNOFILE=65535

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=${BASE_DIR}

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${APP_NAME}"
}

switch_release() {
  log "Switching release atomically"
  ln -sfn "${NEW_RELEASE}" "${BASE_DIR}/current_tmp"
  mv -Tf "${BASE_DIR}/current_tmp" "${CURRENT_LINK}"
  chown -h "${APP_USER}:${APP_USER}" "${CURRENT_LINK}"
}

restart_and_verify() {
  log "Restarting ${APP_NAME}"
  systemctl restart "${APP_NAME}"
  sleep 5
  verify_service
}

cleanup() {
  log "Cleaning old releases"

  CURRENT_REAL="$(readlink -f "${CURRENT_LINK}" || true)"

  find "${RELEASES_DIR}" -mindepth 1 -maxdepth 1 -type d \
    ! -path "${CURRENT_REAL}" \
    ! -path "${PREVIOUS_RELEASE}" \
    | sort -r \
    | tail -n +"$((KEEP_RELEASES + 1))" \
    | xargs -r rm -rf
}

main() {
  require_root
  install_packages
  ensure_user
  ensure_dirs
  ensure_env

  exec 9>"${LOCK_FILE}"
  flock -n 9 || fail "Another deployment is already running"

  if [[ -L "${CURRENT_LINK}" ]]; then
    PREVIOUS_RELEASE="$(readlink -f "${CURRENT_LINK}")"
    log "Previous release: ${PREVIOUS_RELEASE}"
  fi

  trap rollback ERR

  ensure_source_repo
  prepare_release
  setup_venv
  install_playwright_browsers
  ensure_service
  switch_release
  restart_and_verify
  cleanup

  trap - ERR
  log "DEPLOY SUCCESS: ${NEW_RELEASE}"
}

main "$@"
