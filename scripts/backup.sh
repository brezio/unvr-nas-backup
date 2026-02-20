#!/usr/bin/env bash
set -euo pipefail

# ── Load environment (cron doesn't inherit container env) ────────────────────
# shellcheck disable=SC1091
[ -f /etc/environment ] && set -a && . /etc/environment && set +a

# ── Defaults ─────────────────────────────────────────────────────────────────
PROTECT_SSH_USER="${PROTECT_SSH_USER:-root}"
PROTECT_SSH_PORT="${PROTECT_SSH_PORT:-22}"
PROTECT_VIDEO_PATH="${PROTECT_VIDEO_PATH:-/srv/unifi-protect/video}"
PROTECT_DB_PORT="${PROTECT_DB_PORT:-5433}"
PROTECT_DB_NAME="${PROTECT_DB_NAME:-unifi-protect}"
BACKUP_HOURS="${BACKUP_HOURS:-1}"
BATCH_SIZE="${BATCH_SIZE:-5}"
BATCH_DELAY="${BATCH_DELAY:-30}"
LOG_LEVEL="${LOG_LEVEL:-info}"
SSH_DIR="${SSH_DIR:-/tmp/.ssh-copy}"

STAGING_DIR="/staging"
REMUX_DIR="${STAGING_DIR}/remuxed"
ARCHIVE_DIR="/archive"
LOCKFILE="/tmp/backup.lock"

# ── Helpers ──────────────────────────────────────────────────────────────────
log()   { echo "[backup] $(date '+%Y-%m-%d %H:%M:%S') $*"; }
debug() { [ "$LOG_LEVEL" = "debug" ] && log "DEBUG: $*"; return 0; }
warn()  { log "WARN: $*"; }
die()   { log "ERROR: $*" >&2; exit 1; }

# ── Lock ─────────────────────────────────────────────────────────────────────
if [ -f "$LOCKFILE" ]; then
    pid=$(cat "$LOCKFILE" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        die "Another backup is running (PID $pid). Skipping."
    fi
    warn "Stale lockfile found, removing"
    rm -f "$LOCKFILE"
fi
echo $$ > "$LOCKFILE"

# ── Cleanup trap ─────────────────────────────────────────────────────────────
cleanup() {
    local exit_code=$?
    log "Cleaning up staging files..."
    rm -rf "${STAGING_DIR:?}/ubv" "${STAGING_DIR:?}/remuxed" "${STAGING_DIR:?}"/*.meta
    rm -f "$LOCKFILE"
    if [ $exit_code -eq 0 ]; then
        log "Backup completed successfully"
    else
        log "Backup exited with code $exit_code"
    fi
}
trap cleanup EXIT

# ── Prep staging dirs ────────────────────────────────────────────────────────
mkdir -p "${STAGING_DIR}/ubv" "$REMUX_DIR"

# ── SSH / SCP helpers ────────────────────────────────────────────────────────
# SCP uses -P (uppercase) for port, not -p (which means "preserve times")
SCP_OPTS=$(echo "$SSH_OPTS" | sed 's/-p /-P /g')

remote() {
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "${PROTECT_SSH_USER}@${PROTECT_HOST}" "$@"
}

remote_scp() {
    # shellcheck disable=SC2086
    scp $SCP_OPTS "$@"
}

# ── Step 1: Query DB for recent recording files ─────────────────────────────
log "Querying recordings from the last ${BACKUP_HOURS}h..."

LOOKBACK_MS=$((BACKUP_HOURS * 3600 * 1000))

CSV=$(remote "psql -p ${PROTECT_DB_PORT} -U postgres -d ${PROTECT_DB_NAME} -At" <<EOSQL
COPY (
    SELECT c.name, rf.file, rf.folder, rf.start, rf."end", rf.channel
    FROM cameras c
    JOIN "recordingFiles" rf ON c.id = rf."cameraId"
    WHERE rf.type = 'rotating'
      AND rf.active = false
      AND rf.start > (extract(epoch from now()) * 1000 - ${LOOKBACK_MS})
    ORDER BY rf.start ASC
) TO STDOUT WITH CSV HEADER;
EOSQL
) || die "DB query failed"

# Count results (subtract header line)
TOTAL=$(echo "$CSV" | tail -n +2 | grep -c . || true)
if [ "$TOTAL" -eq 0 ]; then
    log "No new recordings found. Done."
    exit 0
fi
log "Found ${TOTAL} recording file(s) to back up"

# ── Step 2: Copy .ubv files via SCP in batches ──────────────────────────────
log "Copying .ubv files (batch size=${BATCH_SIZE}, delay=${BATCH_DELAY}s)..."

copied=0
skipped=0
batch_count=0

while IFS=',' read -r cam_name file folder start_ts end_ts channel; do
    # Build remote path
    ubv_path="${folder}/${file}"
    local_ubv="${STAGING_DIR}/ubv/${file}"

    # Build final archive path to check for duplicates
    start_sec=$((start_ts / 1000))
    end_sec=$((end_ts / 1000))
    date_str=$(date -u -d "@${start_sec}" '+%Y-%m-%d')
    start_time=$(date -u -d "@${start_sec}" '+%H-%M-%S')
    end_time=$(date -u -d "@${end_sec}" '+%H-%M-%S')
    safe_cam=$(echo "$cam_name" | tr ' /' '_-')
    final_name="${safe_cam}_${date_str}_${start_time}_to_${end_time}.mp4"
    archive_subdir="${ARCHIVE_DIR}/${safe_cam}/${date_str}"
    final_path="${archive_subdir}/${final_name}"

    # Skip if already archived
    if [ -f "$final_path" ]; then
        debug "Already archived: ${final_name}"
        skipped=$((skipped + 1))
        continue
    fi

    debug "Copying: ${ubv_path}"
    remote_scp "${PROTECT_SSH_USER}@${PROTECT_HOST}:${ubv_path}" "$local_ubv" || {
        warn "Failed to copy ${ubv_path}, skipping"
        continue
    }

    # Write metadata sidecar (quote values for safe sourcing)
    cat > "${STAGING_DIR}/${file}.meta" <<METAEOF
cam_name='${cam_name//\'/\'\\\'\'}'
start_ts='${start_ts}'
end_ts='${end_ts}'
channel='${channel}'
METAEOF

    copied=$((copied + 1))
    batch_count=$((batch_count + 1))

    # Pause between batches to avoid overwhelming the CloudKey
    if [ "$batch_count" -ge "$BATCH_SIZE" ]; then
        debug "Batch pause: ${BATCH_DELAY}s"
        sleep "$BATCH_DELAY"
        batch_count=0
    fi
done < <(echo "$CSV" | tail -n +2)

log "Copied ${copied} file(s), skipped ${skipped} already-archived"

# ── Step 3: Remux .ubv → .mp4 ───────────────────────────────────────────────
log "Remuxing .ubv files to .mp4..."
remuxed=0
failed=0

for ubv_file in "${STAGING_DIR}/ubv/"*.ubv; do
    [ -f "$ubv_file" ] || continue

    basename_ubv=$(basename "$ubv_file")
    debug "Remuxing: ${basename_ubv}"

    if /usr/local/bin/remux --output-folder="$REMUX_DIR" --fast-start true "$ubv_file" 2>&1; then
        remuxed=$((remuxed + 1))
    else
        warn "Remux failed for ${basename_ubv}"
        failed=$((failed + 1))
    fi
done

log "Remuxed ${remuxed} file(s), ${failed} failed"

# ── Step 4: Rename and archive ───────────────────────────────────────────────
log "Archiving .mp4 files..."
archived=0

for meta_file in "${STAGING_DIR}/"*.meta; do
    [ -f "$meta_file" ] || continue

    # shellcheck disable=SC1090
    . "$meta_file"

    # The original .ubv filename (from the meta sidecar)
    ubv_basename=$(basename "$meta_file" .meta)

    # Find the remuxed .mp4 — remux outputs files based on the input name
    # The remux tool typically outputs <input_basename>_0_rotating_<timestamp>.mp4
    # but the exact pattern depends on the version; we match by prefix
    mp4_file=""
    ubv_stem="${ubv_basename%.ubv}"
    for candidate in "${REMUX_DIR}/"*.mp4; do
        [ -f "$candidate" ] || continue
        candidate_base=$(basename "$candidate")
        if [[ "$candidate_base" == "${ubv_stem}"* ]]; then
            mp4_file="$candidate"
            break
        fi
    done

    if [ -z "$mp4_file" ]; then
        # Fallback: check if remux output matches exactly
        if [ -f "${REMUX_DIR}/${ubv_stem}.mp4" ]; then
            mp4_file="${REMUX_DIR}/${ubv_stem}.mp4"
        else
            warn "No .mp4 found for ${ubv_basename} — skipping"
            continue
        fi
    fi

    # Build archive path
    start_sec=$((start_ts / 1000))
    end_sec=$((end_ts / 1000))
    date_str=$(date -u -d "@${start_sec}" '+%Y-%m-%d')
    start_time=$(date -u -d "@${start_sec}" '+%H-%M-%S')
    end_time=$(date -u -d "@${end_sec}" '+%H-%M-%S')
    safe_cam=$(echo "$cam_name" | tr ' /' '_-')

    final_name="${safe_cam}_${date_str}_${start_time}_to_${end_time}.mp4"
    archive_subdir="${ARCHIVE_DIR}/${safe_cam}/${date_str}"

    mkdir -p "$archive_subdir"
    mv "$mp4_file" "${archive_subdir}/${final_name}"
    debug "Archived: ${archive_subdir}/${final_name}"
    archived=$((archived + 1))
done

log "Archived ${archived} file(s) to ${ARCHIVE_DIR}"

# ── Summary ──────────────────────────────────────────────────────────────────
log "Done — queried=${TOTAL} copied=${copied} remuxed=${remuxed} archived=${archived}"
