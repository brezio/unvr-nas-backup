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
BATCH_SIZE="${BATCH_SIZE:-20}"
BATCH_DELAY="${BATCH_DELAY:-5}"
LOG_LEVEL="${LOG_LEVEL:-info}"
BACKUP_CHANNELS="${BACKUP_CHANNELS:-0}"
REMUX_JOBS="${REMUX_JOBS:-4}"
SSH_DIR="${SSH_DIR:-/tmp/.ssh-copy}"
RETENTION_DAYS="${RETENTION_DAYS:-}"
RETENTION_PERCENT="${RETENTION_PERCENT:-}"
S3_ENABLED="${S3_ENABLED:-false}"
S3_BUCKET="${S3_BUCKET:-}"
S3_PREFIX="${S3_PREFIX:-}"
S3_REGION="${S3_REGION:-us-east-1}"
S3_STORAGE_CLASS="${S3_STORAGE_CLASS:-STANDARD}"
S3_ENDPOINT_URL="${S3_ENDPOINT_URL:-}"
S3_DELETE_LOCAL="${S3_DELETE_LOCAL:-false}"

STAGING_DIR="/staging"
REMUX_DIR="${STAGING_DIR}/remuxed"
ARCHIVE_DIR="/archive"
INDEX_FILE="${ARCHIVE_DIR}/_index.json"
LOCKFILE="/shared/backup.lock"
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
        archive_subdir="${ARCHIVE_DIR}/by-id/${safe_cam}/${date_str}"
    else
        final_name="${safe_cam}_${date_str}_${start_time}_to_${end_time}_${label}.mp4"
        archive_subdir="${ARCHIVE_DIR}/by-id/${safe_cam}/${label}/${date_str}"
    fi
    final_path="${archive_subdir}/${final_name}"
}

# ── Retention helpers ────────────────────────────────────────────────────────
prune_date_dir() {
    local date_dir="$1"
    local bydate_path="${ARCHIVE_DIR}/by-date/${date_dir}"
    [ -d "$bydate_path" ] || return 0

    local count=0
    for link in "$bydate_path"/*; do
        [ -L "$link" ] || continue
        local target
        target=$(readlink -f "$link")
        if [ -d "$target" ]; then
            count=$(( count + $(find "$target" -type f | wc -l) ))
            rm -rf "$target"
            # Clean empty parents up to by-id/
            local parent
            parent=$(dirname "$target")
            rmdir "$parent" 2>/dev/null || true
            parent=$(dirname "$parent")
            rmdir "$parent" 2>/dev/null || true
        fi
        rm -f "$link"
    done
    rmdir "$bydate_path" 2>/dev/null || true
    log "Pruned ${date_dir}: removed ${count} file(s)"
}

run_retention_prune() {
    local date_dirs
    date_dirs=$(ls -1 "${ARCHIVE_DIR}/by-date/" 2>/dev/null | sort)
    [ -n "$date_dirs" ] || return 0

    # Phase 1: RETENTION_DAYS — delete everything older than N days
    if [ -n "$RETENTION_DAYS" ]; then
        local cutoff
        cutoff=$(date -u -d "-${RETENTION_DAYS} days" '+%Y-%m-%d')
        log "Retention: pruning footage older than ${cutoff} (${RETENTION_DAYS} days)"
        while IFS= read -r d; do
            [[ "$d" < "$cutoff" ]] || continue
            prune_date_dir "$d"
        done <<< "$date_dirs"
        # Refresh list after phase 1
        date_dirs=$(ls -1 "${ARCHIVE_DIR}/by-date/" 2>/dev/null | sort)
    fi

    # Phase 2: RETENTION_PERCENT — delete oldest until disk usage drops below threshold
    if [ -n "$RETENTION_PERCENT" ]; then
        local usage
        usage=$(df --output=pcent "$ARCHIVE_DIR" | tail -1 | tr -dc '0-9')
        if [ "${usage:-0}" -gt "$RETENTION_PERCENT" ]; then
            log "Retention: disk at ${usage}%, target <${RETENTION_PERCENT}%"
            while IFS= read -r d; do
                [ -d "${ARCHIVE_DIR}/by-date/${d}" ] || continue
                prune_date_dir "$d"
                usage=$(df --output=pcent "$ARCHIVE_DIR" | tail -1 | tr -dc '0-9')
                if [ "${usage:-0}" -le "$RETENTION_PERCENT" ]; then
                    log "Retention: disk now at ${usage}%, below target"
                    break
                fi
            done <<< "$date_dirs"
            usage=$(df --output=pcent "$ARCHIVE_DIR" | tail -1 | tr -dc '0-9')
            if [ "${usage:-0}" -gt "$RETENTION_PERCENT" ]; then
                warn "Retention: disk still at ${usage}% after pruning all available dates"
            fi
        else
            debug "Retention: disk at ${usage}%, below ${RETENTION_PERCENT}% threshold"
        fi
    fi
}

# ── S3 helpers ──────────────────────────────────────────────────────────────
s3_build_args() {
    local args="--region ${S3_REGION}"
    [ -n "$S3_ENDPOINT_URL" ] && args="${args} --endpoint-url ${S3_ENDPOINT_URL}"
    echo "$args"
}

s3_sync() {
    local local_dir="$1"
    local bucket_prefix="${S3_PREFIX:+${S3_PREFIX}/}"
    local s3_uri="s3://${S3_BUCKET}/${bucket_prefix}by-id/"

    # Configure max concurrent requests for parallel uploads
    aws configure set default.s3.max_concurrent_requests 20

    # shellcheck disable=SC2086
    aws s3 sync "${local_dir}/" "$s3_uri" \
        --storage-class "$S3_STORAGE_CLASS" \
        --size-only \
        --no-follow-symlinks \
        $(s3_build_args) \
        --only-show-errors
}

s3_verify_file() {
    local s3_key="$1" expected_size="$2"
    local bucket_prefix="${S3_PREFIX:+${S3_PREFIX}/}"
    local remote_size
    # shellcheck disable=SC2086
    remote_size=$(aws s3api head-object \
        --bucket "$S3_BUCKET" \
        --key "${bucket_prefix}${s3_key}" \
        $(s3_build_args) \
        --query ContentLength --output text 2>/dev/null) || return 1
    [ "$remote_size" = "$expected_size" ]
}

delete_local_file() {
    local file_path="$1"
    [ -f "$file_path" ] || return 0
    rm -f "$file_path"
    debug "Deleted local file: ${file_path}"
    # Clean up empty parent directories up to the archive root
    local parent
    parent=$(dirname "$file_path")
    while [ "$parent" != "$ARCHIVE_DIR" ] && [ "$parent" != "/" ]; do
        rmdir "$parent" 2>/dev/null || break
        parent=$(dirname "$parent")
    done
}

# ── Read camera index (allow-list) ──────────────────────────────────────────
# _index.json at the archive root controls which cameras are backed up.
# If the file is missing or its cameras array is empty, all cameras are included.
# Only cameras with "enabled": true are backed up during scheduled runs.
# API-triggered backups with an explicit BACKUP_CAMERA_ID bypass this filter.
INDEX_CAMERA_IDS=""
if [ -z "${BACKUP_CAMERA_ID:-}" ] && [ -f "$INDEX_FILE" ]; then
    # Extract enabled camera IDs using python3 (already in the container)
    INDEX_CAMERA_IDS=$(python3 -c "
import json, sys
try:
    data = json.load(open('${INDEX_FILE}'))
    ids = [c['id'] for c in data.get('cameras', []) if c.get('enabled', True)]
    if ids:
        print(','.join(\"'\" + i + \"'\" for i in ids))
except Exception:
    pass
" 2>/dev/null) || true
    if [ -n "$INDEX_CAMERA_IDS" ]; then
        debug "Camera index: filtering to $(echo "$INDEX_CAMERA_IDS" | tr -cd ',' | wc -c | awk '{print $1+1}') enabled camera(s)"
    fi
fi

# ── Validate BACKUP_CHANNELS → SQL IN clause ────────────────────────────────
CHANNEL_LIST=""
IFS=',' read -ra _channels <<< "$BACKUP_CHANNELS"
for _ch in "${_channels[@]}"; do
    _ch=$(echo "$_ch" | tr -dc '0-9')
    [ -n "$_ch" ] && CHANNEL_LIST="${CHANNEL_LIST:+$CHANNEL_LIST,}$_ch"
done
unset _channels _ch
[ -n "$CHANNEL_LIST" ] || die "BACKUP_CHANNELS is empty or invalid: ${BACKUP_CHANNELS}"

# ── Validate retention settings ─────────────────────────────────────────────
if [ -n "${RETENTION_DAYS}" ]; then
    [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || die "RETENTION_DAYS must be a positive integer"
    [ "$RETENTION_DAYS" -gt 0 ] || die "RETENTION_DAYS must be > 0"
fi
if [ -n "${RETENTION_PERCENT}" ]; then
    [[ "$RETENTION_PERCENT" =~ ^[0-9]+$ ]] || die "RETENTION_PERCENT must be a positive integer"
    [ "$RETENTION_PERCENT" -gt 0 ] && [ "$RETENTION_PERCENT" -le 99 ] || die "RETENTION_PERCENT must be 1-99"
fi

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
# Optional overrides (set by API trigger, not intended for .env)
BACKUP_CAMERA_ID="${BACKUP_CAMERA_ID:-}"
BACKUP_START="${BACKUP_START:-}"
BACKUP_END="${BACKUP_END:-}"

# Build time range filter
if [ -n "$BACKUP_START" ] || [ -n "$BACKUP_END" ]; then
    TIME_FILTER=""
    if [ -n "$BACKUP_START" ]; then
        [[ "$BACKUP_START" =~ ^[0-9]+$ ]] || die "BACKUP_START must be epoch milliseconds"
        TIME_FILTER="AND rf.\"end\" > ${BACKUP_START}"
    fi
    if [ -n "$BACKUP_END" ]; then
        [[ "$BACKUP_END" =~ ^[0-9]+$ ]] || die "BACKUP_END must be epoch milliseconds"
        TIME_FILTER="${TIME_FILTER} AND rf.start < ${BACKUP_END}"
    fi
    log "Querying recordings (time range: start=${BACKUP_START:-any} end=${BACKUP_END:-any}, channels: ${CHANNEL_LIST})..."
else
    LOOKBACK_MS=$((BACKUP_HOURS * 3600 * 1000))
    TIME_FILTER="AND rf.\"end\" > (extract(epoch from now()) * 1000 - ${LOOKBACK_MS})"
    log "Querying recordings from the last ${BACKUP_HOURS}h (channels: ${CHANNEL_LIST})..."
fi

# Build optional camera filter
CAMERA_FILTER=""
if [ -n "$BACKUP_CAMERA_ID" ]; then
    # Validate: camera IDs are hex strings (Protect uses 24-char hex ObjectIds)
    [[ "$BACKUP_CAMERA_ID" =~ ^[a-fA-F0-9]+$ ]] || die "BACKUP_CAMERA_ID must be a valid hex ID"
    CAMERA_FILTER="AND c.id = '${BACKUP_CAMERA_ID}'"
    log "Filtering to camera: ${BACKUP_CAMERA_ID}"
elif [ -n "$INDEX_CAMERA_IDS" ]; then
    # Use the camera index allow-list
    CAMERA_FILTER="AND c.id IN (${INDEX_CAMERA_IDS})"
    log "Filtering to cameras from _index.json"
fi

CSV=$(remote "psql -p ${PROTECT_DB_PORT} -U postgres -d ${PROTECT_DB_NAME} -At" <<EOSQL
COPY (
    SELECT c.name, rf.file, rf.folder, rf.start, rf."end", rf.channel, c.id
    FROM cameras c
    JOIN "recordingFiles" rf ON c.id = rf."cameraId"
    WHERE rf.type = 'rotating'
      AND rf.active = false
      ${TIME_FILTER}
      ${CAMERA_FILTER}
      AND rf.channel IN (${CHANNEL_LIST})
    ORDER BY rf."end" ASC
) TO STDOUT WITH CSV HEADER;
EOSQL
) || die "DB query failed"

# Count results (subtract header line)
TOTAL=$(echo "$CSV" | tail -n +2 | grep -c . || true)
if [ "$TOTAL" -eq 0 ]; then
    log "No new recordings found."
    if [ -n "${RETENTION_DAYS}" ] || [ -n "${RETENTION_PERCENT}" ]; then
        run_retention_prune
    fi
    log "Done."
    exit 0
fi
log "Found ${TOTAL} recording file(s) to back up"

# ── Step 2: Copy .ubv files via SCP in batches ──────────────────────────────
log "Copying .ubv files (batch size=${BATCH_SIZE}, delay=${BATCH_DELAY}s)..."

copied=0
skipped=0
batch_count=0

while IFS=',' read -r cam_name file folder start_ts end_ts channel cam_id; do
    # Build remote path
    ubv_path="${folder}/${file}"
    local_ubv="${STAGING_DIR}/ubv/${file}"

    # Build final archive path to check for duplicates (uses camera ID for stable paths)
    build_archive_path "$cam_id" "$start_ts" "$end_ts" "$channel"

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
cam_id='${cam_id//\'/\'\\\'\'}'
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

# ── Step 3: Remux .ubv → .mp4 (parallel) ────────────────────────────────────
log "Remuxing .ubv files to .mp4 (jobs=${REMUX_JOBS})..."
REMUX_OK_COUNT="${STAGING_DIR}/.remux-ok-count"
REMUX_FAIL_COUNT="${STAGING_DIR}/.remux-fail-count"
echo 0 > "$REMUX_OK_COUNT"
echo 0 > "$REMUX_FAIL_COUNT"

remux_one() {
    local ubv_file="$1"
    local basename_ubv
    basename_ubv=$(basename "$ubv_file")

    if /usr/local/bin/remux --output-folder="$REMUX_DIR" --fast-start true "$ubv_file" 2>&1; then
        flock "$REMUX_OK_COUNT" bash -c 'echo $(( $(cat "'"$REMUX_OK_COUNT"'") + 1 )) > "'"$REMUX_OK_COUNT"'"'
    else
        echo "$basename_ubv" >> "$FAILURES_FILE"
        flock "$REMUX_FAIL_COUNT" bash -c 'echo $(( $(cat "'"$REMUX_FAIL_COUNT"'") + 1 )) > "'"$REMUX_FAIL_COUNT"'"'
    fi
}
export -f remux_one
export REMUX_DIR FAILURES_FILE REMUX_OK_COUNT REMUX_FAIL_COUNT

# Use find + xargs for portable parallel execution
find "${STAGING_DIR}/ubv/" -maxdepth 1 -name '*.ubv' -print0 2>/dev/null \
    | xargs -0 -r -P "$REMUX_JOBS" -I{} bash -c 'remux_one "$@"' _ {}

remuxed=$(cat "$REMUX_OK_COUNT")
failed=$(cat "$REMUX_FAIL_COUNT")
rm -f "$REMUX_OK_COUNT" "$REMUX_FAIL_COUNT"

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

    # Build archive path (uses camera ID for stable, rename-safe paths)
    build_archive_path "$cam_id" "$start_ts" "$end_ts" "$channel"

    mkdir -p "$archive_subdir"
    mv "$mp4_file" "${archive_subdir}/${final_name}"
    debug "Archived: ${archive_subdir}/${final_name}"

    # Create by-date symlink
    label=$(channel_label "$channel")
    if [ -z "$label" ]; then
        bydate_link="${ARCHIVE_DIR}/by-date/${date_str}/${safe_cam}"
        bydate_target="../../by-id/${safe_cam}/${date_str}"
    else
        bydate_link="${ARCHIVE_DIR}/by-date/${date_str}/${safe_cam}-${label}"
        bydate_target="../../by-id/${safe_cam}/${label}/${date_str}"
    fi
    if [ ! -L "$bydate_link" ]; then
        mkdir -p "${ARCHIVE_DIR}/by-date/${date_str}"
        ln -s "$bydate_target" "$bydate_link"
        debug "Symlinked: by-date/${date_str}/$(basename "$bydate_link")"
    fi

    archived=$((archived + 1))
done

log "Archived ${archived} file(s) to ${ARCHIVE_DIR}"

# ── Step 5: Sync to S3 (if enabled) ────────────────────────────────────────
s3_synced=false
s3_deleted=0

if [ "$S3_ENABLED" = "true" ] && [ "$archived" -gt 0 ]; then
    log "Syncing archive to S3 (bucket=${S3_BUCKET}, class=${S3_STORAGE_CLASS}, max_concurrent_requests=20)..."

    if s3_sync "${ARCHIVE_DIR}/by-id"; then
        log "S3 sync completed successfully"
        s3_synced=true
    else
        warn "S3 sync failed — local files will be kept"
    fi

    # Post-sync: verify and optionally delete local files that were just archived
    if [ "$s3_synced" = "true" ] && [ "$S3_DELETE_LOCAL" = "true" ]; then
        log "Verifying uploads and deleting local copies..."

        for meta_file in "${STAGING_DIR}/"*.meta; do
            [ -f "$meta_file" ] || continue

            # shellcheck disable=SC1090
            . "$meta_file"

            build_archive_path "$cam_id" "$start_ts" "$end_ts" "$channel"

            # Only process files that exist locally
            [ -f "$final_path" ] || continue

            # S3 key mirrors the by-id/ structure
            s3_key="by-id/${final_path#"${ARCHIVE_DIR}/by-id/"}"
            local_size=$(stat -c%s "$final_path")

            if s3_verify_file "$s3_key" "$local_size"; then
                delete_local_file "$final_path"
                s3_deleted=$((s3_deleted + 1))

                # Clean up the by-date symlink if the target directory is now empty
                label=$(channel_label "$channel")
                if [ -z "$label" ]; then
                    bydate_link="${ARCHIVE_DIR}/by-date/${date_str}/${safe_cam}"
                else
                    bydate_link="${ARCHIVE_DIR}/by-date/${date_str}/${safe_cam}-${label}"
                fi
                if [ -L "$bydate_link" ]; then
                    link_target=$(readlink -f "$bydate_link")
                    if [ -d "$link_target" ] && [ -z "$(ls -A "$link_target" 2>/dev/null)" ]; then
                        rm -f "$bydate_link"
                        rmdir "$link_target" 2>/dev/null || true
                        bydate_parent=$(dirname "$bydate_link")
                        rmdir "$bydate_parent" 2>/dev/null || true
                        debug "Cleaned up empty symlink: ${bydate_link}"
                    fi
                fi
            else
                warn "S3 verification failed for ${s3_key} — keeping local copy"
            fi
        done

        log "S3: local files deleted=${s3_deleted}"
    fi
fi

# ── Retention pruning ───────────────────────────────────────────────────────
if [ -n "${RETENTION_DAYS}" ] || [ -n "${RETENTION_PERCENT}" ]; then
    run_retention_prune
fi

# ── Summary ──────────────────────────────────────────────────────────────────
if [ "$S3_ENABLED" = "true" ]; then
    log "Done — queried=${TOTAL} copied=${copied} remuxed=${remuxed} archived=${archived} s3_synced=${s3_synced} s3_deleted=${s3_deleted}"
else
    log "Done — queried=${TOTAL} copied=${copied} remuxed=${remuxed} archived=${archived}"
fi
date +%s > /shared/backup-last-success
