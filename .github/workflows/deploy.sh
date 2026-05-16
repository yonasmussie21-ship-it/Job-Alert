#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="amazon-bot"
APP_ROOT="/home/ubuntu/Job-Alert"
REPO_DIR="$APP_ROOT/repo"
RELEASES_DIR="$APP_ROOT/releases"
SHARED_DIR="$APP_ROOT/shared"
CURRENT_LINK="$APP_ROOT/current"
LOCK_FILE="/tmp/${APP_NAME}-deploy.lock"

HEALTH_URL="http://localhost:3000/health"
KEEP_RELEASES=5
MIN_FREE_KB=1048576

START_TIME=$(date +%s)

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

show_logs() {
  sudo journalctl -u "$APP_NAME" -n 100 --no-pager || true
}

fail() {
  log "ERROR: $*"
  show_logs
  exit 2
}

cleanup_on_error() {
  log "Unexpected deployment failure"
  show_logs
}

trap cleanup_on_error ERR

health_check() {
  for i in {1..30}; do
    if curl -fsS "$HEALTH_URL" > /dev/null; then
      return 0
    fi

    log "Waiting for health... ($i/30)"
    sleep 2
  done

  return 1
}

safe_rm_rf() {
  local target="$1"
  local target_real
  local releases_real

  [ -n "$target" ] || fail "Refusing to remove empty path"

  target_real="$(readlink -f "$target")"
  releases_real="$(readlink -f "$RELEASES_DIR")"

  case "$target_real" in
    "$releases_real"/*) ;;
    *) fail "Unsafe delete: $target_real" ;;
  esac

  [ "$target_real" != "$releases_real" ] || fail "Refusing to delete releases directory"

  rm -rf "$target_real"
}

rollback() {
  trap - ERR

  local previous_release="$1"

  if [ -z "$previous_release" ] || [ ! -d "$previous_release" ]; then
    log "No valid previous release to rollback"
    show_logs
    exit 2
  fi

  log "Rolling back to: $previous_release"

  ln -sfn "$previous_release" "$CURRENT_LINK"

  if ! timeout 30 sudo systemctl restart "$APP_NAME"; then
    log "Rollback restart failed"
    show_logs
    exit 2
  fi

  if health_check; then
    log "Rollback successful"
    exit 1
  fi

  log "Rollback health check failed"
  show_logs
  exit 2
}

cleanup_old_releases() {
  local current_real=""
  local previous_real="${1:-}"

  [ -L "$CURRENT_LINK" ] && current_real="$(readlink -f "$CURRENT_LINK")"
  [ -n "$previous_real" ] && previous_real="$(readlink -f "$previous_real")"

  find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d \
    | sort -r \
    | while read -r release; do
        release_real="$(readlink -f "$release")"

        if [ "$release_real" = "$current_real" ] || [ "$release_real" = "$previous_real" ]; then
          continue
        fi

        echo "$release"
      done \
    | tail -n +"$KEEP_RELEASES" \
    | while read -r old_release; do
        log "Removing old release: $old_release"
        safe_rm_rf "$old_release"
      done
}

main() {
  exec 9>"$LOCK_FILE"

  if ! flock -n 9; then
    log "Another deployment is already running"
    exit 1
  fi

  mkdir -p "$RELEASES_DIR" "$SHARED_DIR"

  [ -d "$REPO_DIR/.git" ] || fail "Invalid repo directory: $REPO_DIR"
  [ -f "$SHARED_DIR/.env" ] || fail "Missing shared .env: $SHARED_DIR/.env"

  systemctl cat "$APP_NAME" > /dev/null || fail "Systemd service does not exist: $APP_NAME"

  AVAILABLE_KB="$(df --output=avail "$APP_ROOT" | tail -1 | tr -d ' ')"

  if [ "$AVAILABLE_KB" -lt "$MIN_FREE_KB" ]; then
    fail "Insufficient disk space. Available KB: $AVAILABLE_KB"
  fi

  COMMIT_SHA="$(cd "$REPO_DIR" && git rev-parse HEAD)"
  NEW_RELEASE="$RELEASES_DIR/$COMMIT_SHA"
  PREVIOUS_RELEASE=""

  [ -L "$CURRENT_LINK" ] && PREVIOUS_RELEASE="$(readlink -f "$CURRENT_LINK")"

  log "Deploying commit: $COMMIT_SHA"

  if [ -d "$NEW_RELEASE" ]; then
    log "Removing existing incomplete release: $NEW_RELEASE"
    safe_rm_rf "$NEW_RELEASE"
  fi

  mkdir -p "$NEW_RELEASE"

  log "Copying application files"
  rsync -a --delete \
    --exclude=".git" \
    --exclude="releases" \
    --exclude="shared" \
    --exclude="current" \
    --exclude="venv" \
    --exclude="__pycache__" \
    --exclude=".pytest_cache" \
    --exclude=".mypy_cache" \
    --exclude=".ruff_cache" \
    "$REPO_DIR/" "$NEW_RELEASE/"

  ln -sfn "$SHARED_DIR/.env" "$NEW_RELEASE/.env"

  cd "$NEW_RELEASE"

  log "Creating virtual environment"
  python3 -m venv venv
  source venv/bin/activate

  log "Installing dependencies"
  timeout 300 pip install --upgrade pip
  timeout 300 pip install --no-cache-dir -r requirements.txt

  log "Running syntax preflight"
  python -m compileall . > /dev/null

  if [ -d tests/smoke ]; then
    log "Running smoke tests"
    python -m pytest tests/smoke -q
  fi

  log "Switching release"
  ln -sfn "$NEW_RELEASE" "$CURRENT_LINK"

  log "Restarting service"
  if ! timeout 30 sudo systemctl restart "$APP_NAME"; then
    rollback "$PREVIOUS_RELEASE"
  fi

  log "Running health check"
  if ! health_check; then
    rollback "$PREVIOUS_RELEASE"
  fi

  cleanup_old_releases "$PREVIOUS_RELEASE"

  END_TIME=$(date +%s)
  log "Deploy time: $((END_TIME - START_TIME)) seconds"
  log "Deployment successful: $COMMIT_SHA"
}

main "$@"
