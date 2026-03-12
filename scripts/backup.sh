#!/usr/bin/env bash
set -euo pipefail

# ── Load environment (cron doesn't inherit container env) ────────────────────
# shellcheck disable=SC1091
[ -f /etc/environment ] && set -a && . /etc/environment && set +a

# ── Defaults ─────────────────────────────────────────────────────────────────
PROTECT_SSH_USER="${PROTECT_SSH_USER:-root}"
PROTECT_VIDEO_PATH="${PROTECT_VIDEO_PATH:-/srv/unifi-protect/video}"
PROTECT_DB_PORT="${PROTECT_DB_PORT:-5433}"
PROTECT_DB_NAME="${PROTECT_DB_NAME:-unifi-protect}"
BACKUP_HOURS="${BACKUP_HOURS:-1}"
BATCH_SIZE="${BATCH_SIZE:-5}"
BATCH_DELAY="${BATCH_DELAY:-30}"
LOG_LEVEL="${LOG_LEVEL:-info}"
BACKUP_CHANNELS="${BACKUP_CHANNELS:-0}"
SSH_DIR="${SSH_DIR:-/tmp/.ssh-copy}"

STAGING_DIR="/staging"
REMUX_DIR="${STAGING_DIR}/remuxed"
ARCHIVE_DIR="/archive"
LOCKFILE="/tmp/backup.lock"
FAILURES_FILE="${STAGING_DIR}/.remux-failures"

# ── Helpers ──────────────────────────────────────────────────────────────────
log()   { echo "[backup] $(date '+%Y-%m-%d %H:%M:%S') $*"; }
debug() { [ "$LOG_LEVEL" = "debug" ] && log "DEBUG: $*"; return 0; }
warn()  { log "WARN: $*"; }
die()   { log "ERROR: $*" >&2; exit 1; }

# Channel label for archive paths (channel 0 = main, no label)
channel_label() {
    case "$1" in
        0) ;;
        2) echo "low-quality" ;;
        *) echo "ch$1" ;;
    esac
}

# Build archive path for a recording.
# Sets globals: archive_subdir, final_name, final_path, date_str, safe_cam
build_archive_path() {
    local cam="$1" start="$2" end="$3" ch="$4"
    local start_sec=$((start / 1000))
    local end_sec=$((end / 1000))
    local start_time end_time label
    date_str=$(date -u -d "@${start_sec}" '+%Y-%m-%d')
    start_time=$(date -u -d "@${start_sec}" '+%H-%M-%S')
    end_time=$(date -u -d "@${end_sec}" '+%H-%M-%S')
    safe_cam=$(echo "$cam" | tr ' /' '_-')
    label=$(channel_label "$ch")

    if [ -z "$label" ]; then
        final_name="${safe_cam}_${date_str}_${start_time}_to_${end_time}.mp4"
        archive_subdir="${ARCHIVE_DIR}/by-camera/${safe_cam}/${date_str}"
    else
        final_name="${safe_cam}_${date_str}_${start_time}_to_${end_time}_${label}.mp4"
        archive_subdir="${ARCHIVE_DIR}/by-camera/${safe_cam}/${label}/${date_str}"
    fi
    final_path="${archive_subdir}/${final_name}"
}

# ── Validate BACKUP_CHANNELS → SQL IN clause ────────────────────────────────
CHANNEL_LIST=""
IFS=',' read -ra _channels <<< "$BACKUP_CHANNELS"
for _ch in "${_channels[@]}"; do
    _ch=$(echo "$_ch" | tr -dc '0-9')
    [ -n "$_ch" ] && CHANNEL_LIST="${CHANNEL_LIST:+$CHANNEL_LIST,}$_ch"
done
unset _channels _ch
[ -n "$CHANNEL_LIST" ] || die "BACKUP_CHANNELS is empty or invalid: ${BACKUP_CHANNELS}"

# ── Lock (atomic via flock) ──────────────────────────────────────────────────
exec 9>"$LOCKFILE"
flock -n 9 || die "Another backup is running. Skipping."

# ── Cleanup trap ─────────────────────────────────────────────────────────────
cleanup() {
    local exit_code=$?
    log "Cleaning up staging files..."
    rm -rf "${STAGING_DIR:?}/ubv" "${STAGING_DIR:?}/remuxed" "${STAGING_DIR:?}"/*.meta
    if [ $exit_code -eq 0 ]; then
        log "Backup completed successfully"
    else
        log "Backup exited with code $exit_code"
    fi
}
trap cleanup EXIT

# ── Prep staging dirs ────────────────────────────────────────────────────────
# Clean up any stale files from a previous interrupted run
rm -rf "${STAGING_DIR}/ubv" "${STAGING_DIR}/remuxed" "${STAGING_DIR}"/*.meta
mkdir -p "${STAGING_DIR}/ubv" "$REMUX_DIR"

# ── Disk space check ────────────────────────────────────────────────────────
archive_avail_kb=$(df -k "$ARCHIVE_DIR" 2>/dev/null | tail -1 | awk '{print $4}')
if [ "${archive_avail_kb:-0}" -lt 10485760 ]; then
    warn "CRITICAL: Archive volume has less than 10 GB free — backups may fail"
elif [ "${archive_avail_kb:-0}" -lt 104857600 ]; then
    warn "Archive volume has less than 100 GB free — consider pruning old recordings"
fi

# ── SSH / SCP helpers ────────────────────────────────────────────────────────
SCP_OPTS="$SSH_OPTS"

remote() {
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "${PROTECT_SSH_USER}@${PROTECT_HOST}" "$@"
}

remote_scp() {
    # shellcheck disable=SC2086
    scp $SCP_OPTS "$@"
}

# ── Step 1: Query DB for recent recording files ─────────────────────────────
log "Querying recordings from the last ${BACKUP_HOURS}h (channels: ${CHANNEL_LIST})..."

LOOKBACK_MS=$((BACKUP_HOURS * 3600 * 1000))

CSV=$(remote "psql -p ${PROTECT_DB_PORT} -U postgres -d ${PROTECT_DB_NAME} -At" <<EOSQL
COPY (
    SELECT c.name, rf.file, rf.folder, rf.start, rf."end", rf.channel
    FROM cameras c
    JOIN "recordingFiles" rf ON c.id = rf."cameraId"
    WHERE rf.type = 'rotating'
      AND rf.active = false
      AND rf."end" > (extract(epoch from now()) * 1000 - ${LOOKBACK_MS})
      AND rf.channel IN (${CHANNEL_LIST})
    ORDER BY rf."end" ASC
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
    build_archive_path "$cam_name" "$start_ts" "$end_ts" "$channel"

    # Skip if already archived
    if [ -f "$final_path" ]; then
        debug "Already archived: ${final_name}"
        skipped=$((skipped + 1))
        continue
    fi

    # Skip if remux previously failed for this file
    if [ -f "$FAILURES_FILE" ] && grep -qFx "$file" "$FAILURES_FILE"; then
        debug "Skipping known remux failure: ${file}"
        skipped=$((skipped + 1))
        continue
    fi

    debug "Copying: ${ubv_path}"
    remote_scp "${PROTECT_SSH_USER}@${PROTECT_HOST}:${ubv_path}" "$local_ubv" 2>/dev/null || {
        # Fallback to PROTECT_VIDEO_PATH if the DB folder path doesn't work
        fallback_path="${PROTECT_VIDEO_PATH}/${file}"
        debug "DB path failed, trying fallback: ${fallback_path}"
        remote_scp "${PROTECT_SSH_USER}@${PROTECT_HOST}:${fallback_path}" "$local_ubv" || {
            warn "Failed to copy ${file}, skipping"
            continue
        }
    }

    # Validate numeric fields from DB
    [[ "$start_ts" =~ ^[0-9]+$ ]] || { warn "Invalid start_ts for ${file}, skipping"; continue; }
    [[ "$end_ts" =~ ^[0-9]+$ ]] || { warn "Invalid end_ts for ${file}, skipping"; continue; }
    [[ "$channel" =~ ^[0-9]+$ ]] || { warn "Invalid channel for ${file}, skipping"; continue; }

    # Write metadata sidecar (quote values for safe sourcing)
    cat > "${STAGING_DIR}/${file}.meta" <<METAEOF
cam_name='${cam_name//\'/\'\\\'\'}'
start_ts='${start_ts}'
end_ts='${end_ts}'
channel='${channel}'
METAEOF

    copied=$((copied + 1))
    batch_count=$((batch_count + 1))

    # Pause between batches to avoid overwhelming the Protect device
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
        echo "$basename_ubv" >> "$FAILURES_FILE"
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

    # Find the remuxed .mp4 — remux replaces the epoch timestamp with an ISO
    # timestamp, so input  "MAC_0_rotating_1771552658462.ubv" becomes
    # output "MAC_0_rotating_2026-02-20T01.57.33Z.mp4". Match on the
    # MAC_channel_type prefix which is shared between input and output.
    mp4_file=""
    # Extract prefix: everything up to and including the type (e.g. "1C6A1B84F76E_0_rotating_")
    ubv_prefix=$(echo "$ubv_basename" | sed 's/\(.*_rotating_\).*/\1/')
    for candidate in "${REMUX_DIR}/"*.mp4; do
        [ -f "$candidate" ] || continue
        candidate_base=$(basename "$candidate")
        if [[ "$candidate_base" == "${ubv_prefix}"* ]]; then
            mp4_file="$candidate"
            break
        fi
    done

    if [ -z "$mp4_file" ]; then
        warn "No .mp4 found for ${ubv_basename} — skipping"
        continue
    fi

    # Build archive path
    build_archive_path "$cam_name" "$start_ts" "$end_ts" "$channel"

    mkdir -p "$archive_subdir"
    mv "$mp4_file" "${archive_subdir}/${final_name}"
    debug "Archived: ${archive_subdir}/${final_name}"

    # Create by-date symlink
    label=$(channel_label "$channel")
    if [ -z "$label" ]; then
        bydate_link="${ARCHIVE_DIR}/by-date/${date_str}/${safe_cam}"
        bydate_target="../../by-camera/${safe_cam}/${date_str}"
    else
        bydate_link="${ARCHIVE_DIR}/by-date/${date_str}/${safe_cam}-${label}"
        bydate_target="../../by-camera/${safe_cam}/${label}/${date_str}"
    fi
    if [ ! -L "$bydate_link" ]; then
        mkdir -p "${ARCHIVE_DIR}/by-date/${date_str}"
        ln -s "$bydate_target" "$bydate_link"
        debug "Symlinked: by-date/${date_str}/$(basename "$bydate_link")"
    fi

    archived=$((archived + 1))
done

log "Archived ${archived} file(s) to ${ARCHIVE_DIR}"

# ── Summary ──────────────────────────────────────────────────────────────────
log "Done — queried=${TOTAL} copied=${copied} remuxed=${remuxed} archived=${archived}"
date +%s > /tmp/backup-last-success
