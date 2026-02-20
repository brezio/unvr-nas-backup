#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

echo "=== Updating unvr-nas-backup ==="

echo "Pulling latest code..."
git pull

echo
echo "Rebuilding Docker image..."
docker compose build

echo
echo "Restarting container..."
docker compose down
docker compose up -d

echo
echo "Done! Checking status..."
docker compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Health}}'
