#!/usr/bin/env bash
# Daily off-server backup of every business's data.
#
#   1. Takes a consistent snapshot of all tenant databases (+ config/tenants.yaml) from the running app.
#   2. Encrypts it with BACKUP_PASSPHRASE (gpg, AES-256).
#   3. Checks the encrypted file can be decrypted and read back.
#   4. Copies it off the server with rclone to BACKUP_REMOTE.
#   5. Emails ALERT_EMAIL if any step fails.
#
# Settings (in .env): BACKUP_PASSPHRASE, BACKUP_REMOTE (required);
#   BACKUP_LOCAL_DIR (default /var/backups/agentic-sme), BACKUP_LOCAL_KEEP_DAYS (default 14).
# Remote copies are never deleted by this script.
#
# Schedule daily at 03:15 (crontab -e):
#   15 3 * * * /path/to/Agentic-SME-SAAS/scripts/backup.sh >> /var/log/agentic-backup.log 2>&1
set -Eeuo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_DIR/.env}"
COMPOSE="${COMPOSE:-docker compose --env-file $ENV_FILE -f $REPO_DIR/docker/docker-compose.yml}"

# Reads one KEY=value from .env without executing the file.
env_value() {
  local key="$1"
  [ -f "$ENV_FILE" ] || return 0
  { grep -E "^${key}=" "$ENV_FILE" || true; } | tail -n 1 | cut -d= -f2- \
    | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE:-$(env_value BACKUP_PASSPHRASE)}"
BACKUP_REMOTE="${BACKUP_REMOTE:-$(env_value BACKUP_REMOTE)}"
BACKUP_LOCAL_DIR="${BACKUP_LOCAL_DIR:-$(env_value BACKUP_LOCAL_DIR)}"
BACKUP_LOCAL_DIR="${BACKUP_LOCAL_DIR:-/var/backups/agentic-sme}"
BACKUP_LOCAL_KEEP_DAYS="${BACKUP_LOCAL_KEEP_DAYS:-$(env_value BACKUP_LOCAL_KEEP_DAYS)}"
BACKUP_LOCAL_KEEP_DAYS="${BACKUP_LOCAL_KEEP_DAYS:-14}"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

send_failure_alert() {
  local reason="$1"
  local subject="[Assistant] Daily backup FAILED on $(hostname)"
  local body="The daily backup failed: ${reason}. Check /var/log/agentic-backup.log on the server."
  # Use the running app's container; fall back to a one-off container if the app is down.
  $COMPOSE exec -T gateway python -m src.utils.alerts "$subject" "$body" \
    || $COMPOSE run --rm --no-deps -T gateway python -m src.utils.alerts "$subject" "$body" \
    || log "ERROR: could not send failure alert email either."
}

CURRENT_STEP="starting"
on_error() {
  log "ERROR during: ${CURRENT_STEP}"
  send_failure_alert "$CURRENT_STEP"
  [ -n "${OUT_FILE:-}" ] && rm -f "$OUT_FILE"
  exit 1
}
trap on_error ERR

CURRENT_STEP="checking settings"
[ -n "$BACKUP_PASSPHRASE" ] || { log "BACKUP_PASSPHRASE is not set in $ENV_FILE"; false; }
[ -n "$BACKUP_REMOTE" ] || { log "BACKUP_REMOTE is not set in $ENV_FILE"; false; }
command -v gpg >/dev/null || { log "gpg is not installed (apt install gnupg)"; false; }
command -v rclone >/dev/null || { log "rclone is not installed (see README: Backups)"; false; }

mkdir -p "$BACKUP_LOCAL_DIR"
chmod 700 "$BACKUP_LOCAL_DIR"
STAMP="$(date -u +%Y%m%d_%H%M%S)"
OUT_FILE="$BACKUP_LOCAL_DIR/agentic-sme-backup_${STAMP}.tar.gz.gpg"

CURRENT_STEP="snapshot and encrypt"
log "Creating encrypted snapshot $OUT_FILE"
$COMPOSE exec -T gateway python -m src.utils.backup_daemon --archive - \
  | gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-fd 3 \
        --symmetric --cipher-algo AES256 -o "$OUT_FILE" 3<<<"$BACKUP_PASSPHRASE"
chmod 600 "$OUT_FILE"

CURRENT_STEP="verifying the backup can be decrypted"
FILE_LIST="$(gpg --batch --quiet --pinentry-mode loopback --passphrase-fd 3 --decrypt "$OUT_FILE" 3<<<"$BACKUP_PASSPHRASE" | tar -tzf -)"
log "Backup contains: $(echo "$FILE_LIST" | tr '\n' ' ')"

CURRENT_STEP="copying off-server to $BACKUP_REMOTE"
rclone copy "$OUT_FILE" "$BACKUP_REMOTE"
rclone lsf "$BACKUP_REMOTE" --include "$(basename "$OUT_FILE")" | grep -q . \
  || { log "Uploaded file not found on remote"; false; }

CURRENT_STEP="removing local copies older than ${BACKUP_LOCAL_KEEP_DAYS} days"
find "$BACKUP_LOCAL_DIR" -name 'agentic-sme-backup_*.tar.gz.gpg' -mtime +"$BACKUP_LOCAL_KEEP_DAYS" -delete

log "Backup OK: $(basename "$OUT_FILE") -> $BACKUP_REMOTE"
