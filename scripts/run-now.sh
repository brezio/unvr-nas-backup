#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

echo "Triggering backup now..."

if ! docker compose ps --format '{{.Name}}' 2>/dev/null | grep -q unvr-nas-backup-exporter; then
    echo "ERROR: Exporter container is not running. Start it with: docker compose up -d" >&2
    exit 1
fi

docker compose exec exporter /usr/local/bin/backup.sh
