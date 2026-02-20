#!/usr/bin/env bash
set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────────────
PROTECT_SSH_USER="${PROTECT_SSH_USER:-root}"
PROTECT_SSH_PORT="${PROTECT_SSH_PORT:-22}"
PROTECT_VIDEO_PATH="${PROTECT_VIDEO_PATH:-/srv/unifi-protect/video}"
PROTECT_DB_PORT="${PROTECT_DB_PORT:-5433}"
PROTECT_DB_NAME="${PROTECT_DB_NAME:-unifi-protect}"
BACKUP_HOURS="${BACKUP_HOURS:-1}"
BATCH_SIZE="${BATCH_SIZE:-5}"
BATCH_DELAY="${BATCH_DELAY:-30}"
CRON_SCHEDULE="${CRON_SCHEDULE:-0 * * * *}"
RUN_ON_START="${RUN_ON_START:-true}"
LOG_LEVEL="${LOG_LEVEL:-info}"
TZ="${TZ:-UTC}"

# ── Helpers ──────────────────────────────────────────────────────────────────
log() { echo "[entrypoint] $(date '+%Y-%m-%d %H:%M:%S') $*"; }

die() { log "ERROR: $*" >&2; exit 1; }

# ── Validate ─────────────────────────────────────────────────────────────────
[ -z "${PROTECT_HOST:-}" ] && die "PROTECT_HOST is required"
[ -d "/archive" ] || die "/archive is not mounted"

# ── SSH key setup ────────────────────────────────────────────────────────────
# The host key directory is mounted read-only, so we copy to a writable location
# and fix permissions (required by OpenSSH).
SSH_DIR="/tmp/.ssh-copy"
rm -rf "$SSH_DIR"
mkdir -p "$SSH_DIR"

if [ -d "/root/.ssh-mount" ]; then
    cp -a /root/.ssh-mount/* "$SSH_DIR/" 2>/dev/null || true
    chmod 700 "$SSH_DIR"
    find "$SSH_DIR" -type f -exec chmod 600 {} \;
    # Ensure known_hosts is writable
    touch "$SSH_DIR/known_hosts"
    chmod 644 "$SSH_DIR/known_hosts"
else
    die "SSH key mount not found at /root/.ssh-mount"
fi

export SSH_DIR

# ── Detect SSH key and test connectivity ─────────────────────────────────────
SSH_BASE_OPTS="-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=${SSH_DIR}/known_hosts -o BatchMode=yes -p ${PROTECT_SSH_PORT}"
SSH_CONNECTED=false

for key_name in id_ed25519 id_rsa id_ecdsa id_dsa; do
    key_path="${SSH_DIR}/${key_name}"
    [ -f "$key_path" ] || continue

    SSH_OPTS="${SSH_BASE_OPTS} -i ${key_path}"
    log "Testing SSH to ${PROTECT_SSH_USER}@${PROTECT_HOST}:${PROTECT_SSH_PORT} (${key_name})..."
    # shellcheck disable=SC2086
    if ssh $SSH_OPTS "${PROTECT_SSH_USER}@${PROTECT_HOST}" "echo ok" >/dev/null 2>&1; then
        log "SSH connection OK (using ${key_name})"
        SSH_CONNECTED=true
        break
    fi
done

if [ "$SSH_CONNECTED" != "true" ]; then
    die "Cannot SSH to ${PROTECT_SSH_USER}@${PROTECT_HOST}:${PROTECT_SSH_PORT} — no working key found"
fi

export SSH_OPTS

# ── Export env for cron ──────────────────────────────────────────────────────
# Cron jobs don't inherit the container's environment, so we dump it to a file
# that backup.sh will source.
# Quote values so sourcing is safe (handles spaces in SSH_OPTS, paths, etc.)
env | grep -E '^(PROTECT_|BACKUP_|BATCH_|ARCHIVE_|SSH_|CRON_|RUN_ON_START|LOG_LEVEL|TZ|PATH=)' \
    | sed "s/=/='/" | sed "s/$/'/" \
    > /etc/environment

# ── Set up cron ──────────────────────────────────────────────────────────────
CRON_LINE="${CRON_SCHEDULE} /usr/local/bin/backup.sh >> /proc/1/fd/1 2>> /proc/1/fd/2"
echo "$CRON_LINE" | crontab -
log "Cron installed: ${CRON_SCHEDULE}"

# ── Optional immediate run ───────────────────────────────────────────────────
if [ "${RUN_ON_START}" = "true" ]; then
    log "Running initial backup..."
    /usr/local/bin/backup.sh || log "WARN: initial backup exited with code $?"
fi

# ── Start cron (foreground, PID 1) ──────────────────────────────────────────
log "Starting cron in foreground"
exec cron -f
