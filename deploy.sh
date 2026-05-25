#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="amazon-bot"
APP_USER="amazonbot"

APP_ROOT="/home/ubuntu/Job-Alert"
REPO_DIR="$APP_ROOT/repo"
RELEASES_DIR="$APP_ROOT/releases"
SHARED_DIR="$APP_ROOT/shared"
CURRENT_LINK="$APP_ROOT/current"
LOCK_FILE="/tmp/${APP_NAME}-deploy.lock"

HEALTH_URL="${HEALTH_URL:-http://localhost:3000/health}"
KEEP_RELEASES=5
MIN_FREE_KB=1048576

START_TIME="$(date +%s)"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

show_logs() {
  sudo journalctl -u "$APP_NAME" -n 120 --no-pager || true
}

fail() {
  log "ERROR: $*"
  show_logs
  exit 2
}

atomic_switch() {
  local target="$1"
  local tmp_link="$APP_ROOT/current_tmp"

  [ -d "$target" ] || fail "Cannot switch to missing release: $target"

  ln -sfn "$target" "$tmp_link"
  mv -Tf "$tmp_link" "$CURRENT_LINK"
}

health_check() {
  for i in {1..30}; do
    if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null; then
      return 0
    fi

    log "Waiting for health... $i/30"
    sleep 2
  done

  return 1
}

safe_rm_rf() {
  local target="$1"
  local target_real
  local releases_real

  [ -n "$target" ] || fail "Refusing empty delete"

  target_real="$(readlink -f "$target")"
  releases_real="$(readlink -f "$RELEASES_DIR")"

  case "$target_real" in
    "$releases_real"/*) ;;
    *) fail "Unsafe delete: $target_real" ;;
  esac

  [ "$target_real" != "$releases_real" ] || fail "Refusing to delete releases dir"

  rm -rf "$target_real"
}

rollback() {
  trap - ERR

  local previous_release="$1"

  if [ -z "$previous_release" ] || [ ! -d "$previous_release" ]; then
    fail "No valid previous release for rollback"
  fi

  log "Rolling back to: $previous_release"
  atomic_switch "$previous_release"

  timeout 30 sudo systemctl restart "$APP_NAME" || fail "Rollback restart failed"

  if health_check; then
    log "Rollback successful"
    exit 1
  fi

  fail "Rollback health check failed"
}

cleanup_old_releases() {
  local previous_real="${1:-}"
  local current_real=""

  [ -L "$CURRENT_LINK" ] && current_real="$(readlink -f "$CURRENT_LINK")"
  [ -n "$previous_real" ] && previous_real="$(readlink -f "$previous_real")"

  find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d \
    | sort -r \
    | while read -r release; do
        release_real="$(readlink -f "$release")"

        [ "$release_real" = "$current_real" ] && continue
        [ "$release_real" = "$previous_real" ] && continue

        echo "$release"
      done \
    | tail -n +"$((KEEP_RELEASES + 1))" \
    | while read -r old_release; do
        log "Removing old release: $old_release"
        safe_rm_rf "$old_release"
      done
}

main() {
  exec 9>"$LOCK_FILE"
  flock -n 9 || fail "Another deployment is already running"

  trap 'log "Unexpected deployment failure"; show_logs' ERR

  mkdir -p "$RELEASES_DIR" "$SHARED_DIR"

  [ -d "$REPO_DIR/.git" ] || fail "Invalid repo directory: $REPO_DIR"
  [ -f "$SHARED_DIR/.env" ] || fail "Missing shared env: $SHARED_DIR/.env"

  systemctl cat "$APP_NAME" >/dev/null || fail "Missing systemd service: $APP_NAME"

  AVAILABLE_KB="$(df --output=avail "$APP_ROOT" | tail -1 | tr -d ' ')"
  [ "$AVAILABLE_KB" -ge "$MIN_FREE_KB" ] || fail "Low disk space"

  cd "$REPO_DIR"

  git fetch origin main
  git reset --hard origin/main
  git clean -fdx

  COMMIT_SHA="$(git rev-parse HEAD)"
  NEW_RELEASE="$RELEASES_DIR/$COMMIT_SHA"
  PREVIOUS_RELEASE=""

  [ -L "$CURRENT_LINK" ] && PREVIOUS_RELEASE="$(readlink -f "$CURRENT_LINK")"

  log "Deploying commit: $COMMIT_SHA"

  if [ -d "$NEW_RELEASE" ]; then
    safe_rm_rf "$NEW_RELEASE"
  fi

  mkdir -p "$NEW_RELEASE"

  git archive "$COMMIT_SHA" | tar -x -C "$NEW_RELEASE"

  echo "$COMMIT_SHA" > "$NEW_RELEASE/.commit"

  ln -sfn "$SHARED_DIR/.env" "$NEW_RELEASE/.env"

  cd "$NEW_RELEASE"

  [ -f requirements.txt ] || fail "requirements.txt missing"
  [ -f main.py ] || fail "main.py missing"

  python3 -m venv venv
  source venv/bin/activate

  timeout 300 pip install --upgrade pip
  timeout 300 pip install --no-cache-dir -r requirements.txt

  python -m compileall . >/dev/null

  if [ -d tests/smoke ]; then
    python -m pytest tests/smoke -q
  fi

  sudo chown -R "$APP_USER:$APP_USER" "$NEW_RELEASE"

  atomic_switch "$NEW_RELEASE"

  if ! timeout 30 sudo systemctl restart "$APP_NAME"; then
    rollback "$PREVIOUS_RELEASE"
  fi

  if ! health_check; then
    rollback "$PREVIOUS_RELEASE"
  fi

  cleanup_old_releases "$PREVIOUS_RELEASE"

  END_TIME="$(date +%s)"

  trap - ERR

  log "Deployment successful: $COMMIT_SHA"
  log "Deploy time: $((END_TIME - START_TIME)) seconds"
}

main "$@"
