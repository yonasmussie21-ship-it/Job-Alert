#!/bin/bash
set -euo pipefail

APP_NAME="amazon-bot"
APP_USER="amazonbot"

BASE_DIR="/opt/${APP_NAME}"
RELEASES_DIR="${BASE_DIR}/releases"
CURRENT_LINK="${BASE_DIR}/current"

log() { echo "[$(date +'%F %T')] $*"; }
fail() { echo "❌ $*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || fail "Run as root"

PREV=$(ls -dt "${RELEASES_DIR}"/* 2>/dev/null | sed -n '2p')

[[ -n "$PREV" ]] || fail "No previous release"

log "Rolling back to $PREV"

ln -sfn "$PREV" "$CURRENT_LINK"
chown -h "${APP_USER}:${APP_USER}" "$CURRENT_LINK"

systemctl restart "${APP_NAME}"

log "✅ Rollback complete"
