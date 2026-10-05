#!/usr/bin/env bash
# Updates the running app to the latest code on DEPLOY_BRANCH (default: main), safely.
#
#   1. Fetches the branch and fast-forwards (never overwrites changes made on the server).
#   2. Rebuilds and restarts the app.
#   3. Waits for the health check. If it fails, goes back to the previous version,
#      restarts that, and emails ALERT_EMAIL.
#
# Run by hand:   ./scripts/deploy.sh
# Run by GitHub: the deploy key in ~/.ssh/authorized_keys is restricted to this script
#                (see README: Automatic updates).
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="${DEPLOY_BRANCH:-main}"
ENV_FILE="${ENV_FILE:-$REPO_DIR/.env}"
COMPOSE="${COMPOSE:-docker compose --env-file $ENV_FILE -f $REPO_DIR/docker/docker-compose.yml}"
HEALTH_CMD="${HEALTH_CMD:-$COMPOSE exec -T gateway curl -fsS http://localhost:8000/health}"
HEALTH_WAIT_SECONDS="${HEALTH_WAIT_SECONDS:-90}"

log() { echo "[deploy $(date -u +%H:%M:%S)] $*"; }

alert() {
  $COMPOSE exec -T gateway python -m src.utils.alerts "[Assistant] Automatic update FAILED on $(hostname)" "$1" \
    || log "could not send alert email"
}

wait_healthy() {
  local waited=0
  until $HEALTH_CMD >/dev/null 2>&1; do
    if [ "$waited" -ge "$HEALTH_WAIT_SECONDS" ]; then
      return 1
    fi
    sleep 5
    waited=$((waited + 5))
  done
}

cd "$REPO_DIR"
exec 9>"${LOCK_FILE:-/tmp/agentic-deploy.lock}"
flock -n 9 || { log "another update is already running"; exit 1; }

PREVIOUS="$(git rev-parse HEAD)"
log "current version ${PREVIOUS:0:7}, updating to latest $BRANCH"
git fetch --quiet origin "$BRANCH"
if [ "$(git rev-parse --abbrev-ref HEAD)" != "$BRANCH" ]; then
  git checkout --quiet "$BRANCH" 2>/dev/null || git checkout --quiet -b "$BRANCH" "origin/$BRANCH"
fi
if ! git merge --ff-only --quiet "origin/$BRANCH"; then
  log "server copy has local code changes that conflict; not updating"
  alert "The server's code has local changes, so the update to $BRANCH was skipped. Nothing was changed."
  exit 1
fi
CURRENT="$(git rev-parse HEAD)"
if [ "$CURRENT" = "$PREVIOUS" ]; then
  log "already up to date (${CURRENT:0:7})"
  exit 0
fi

log "building and restarting ${CURRENT:0:7}"
if $COMPOSE up -d --build && wait_healthy; then
  log "update OK: ${PREVIOUS:0:7} -> ${CURRENT:0:7}"
  exit 0
fi

log "health check failed; rolling back to ${PREVIOUS:0:7}"
git reset --quiet --keep "$PREVIOUS"
$COMPOSE up -d --build || true
if wait_healthy; then
  alert "Update to ${CURRENT:0:7} failed its health check, so the server went back to ${PREVIOUS:0:7}, which is running."
else
  alert "Update to ${CURRENT:0:7} failed and the previous version ${PREVIOUS:0:7} is also not healthy. Check the server."
fi
exit 1
