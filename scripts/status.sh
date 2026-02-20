#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Read specific values from .env (sourcing is unsafe with unquoted values like CRON_SCHEDULE=* * * * *)
if [ -f .env ]; then
    ARCHIVE_PATH=$(grep -m1 '^ARCHIVE_PATH=' .env | cut -d= -f2-)
    CRON_SCHEDULE=$(grep -m1 '^CRON_SCHEDULE=' .env | cut -d= -f2-)
fi

echo "=== unvr-nas-backup status ==="
echo

# Container status
echo "--- Container ---"
if docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | grep -q unvr-nas-backup; then
    docker compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Health}}'
else
    echo "Container is not running."
fi
echo

# Recent logs
echo "--- Last backup ---"
docker compose logs --tail 5 2>/dev/null | grep -E '\[(backup|entrypoint)\]' | tail -5 || echo "No logs available."
echo

# Cron schedule
echo "--- Cron ---"
if [ -n "${CRON_SCHEDULE:-}" ]; then
    echo "Schedule: ${CRON_SCHEDULE}"
else
    echo "Schedule: (check .env)"
fi
echo

# Archive stats
echo "--- Archive ---"
archive="${ARCHIVE_PATH:-/archive}"
by_camera="${archive}/by-camera"
if [ -d "$by_camera" ]; then
    camera_count=$(find "$by_camera" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
    file_count=$(find "$by_camera" -name '*.mp4' -type f 2>/dev/null | wc -l)
    total_size=$(du -sh "$archive" 2>/dev/null | cut -f1)

    echo "Path:    ${archive}"
    echo "Cameras: ${camera_count}"
    echo "Files:   ${file_count} .mp4"
    echo "Size:    ${total_size}"
    echo

    # Per-camera breakdown
    if [ "$camera_count" -gt 0 ]; then
        echo "--- Per camera ---"
        for cam_dir in "$by_camera"/*/; do
            [ -d "$cam_dir" ] || continue
            cam_name=$(basename "$cam_dir")
            cam_files=$(find "$cam_dir" -name '*.mp4' -type f 2>/dev/null | wc -l)
            cam_size=$(du -sh "$cam_dir" 2>/dev/null | cut -f1)
            latest=$(find "$cam_dir" -name '*.mp4' -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
            latest_name=$(basename "$latest" 2>/dev/null || echo "—")
            printf "  %-30s %4d files  %8s  latest: %s\n" "$cam_name" "$cam_files" "$cam_size" "$latest_name"
        done
        echo
    fi
else
    echo "Archive path not found: ${archive}"
    echo
fi

# Disk usage
echo "--- Disk ---"
if [ -d "$archive" ]; then
    df -h "$archive" | tail -1 | awk '{printf "Archive disk: %s used / %s total (%s free, %s used)\n", $3, $2, $4, $5}'
fi
staging_size=$(docker compose exec unvr-nas-backup du -sh /staging 2>/dev/null | cut -f1 || echo "—")
echo "Staging:     ${staging_size}"
