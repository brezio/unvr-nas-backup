#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

echo "Triggering backup now..."

if ! docker compose ps --format '{{.Name}}' 2>/dev/null | grep -q unvr-nas-backup; then
    echo "ERROR: Container is not running. Start it with: docker compose up -d" >&2
    exit 1
fi

docker compose exec unvr-nas-backup /usr/local/bin/backup.sh
