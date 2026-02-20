#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== unvr-nas-backup installer ==="
echo

# Check Docker
if ! command -v docker &>/dev/null; then
    echo "ERROR: docker is not installed." >&2
    exit 1
fi
if ! docker compose version &>/dev/null; then
    echo "ERROR: docker compose is not available." >&2
    exit 1
fi

# Check for existing .env
if [ -f .env ]; then
    echo "Found existing .env file."
    read -rp "Overwrite it? [y/N] " overwrite
    if [[ ! "$overwrite" =~ ^[Yy] ]]; then
        echo "Keeping existing .env. Skipping to build."
        docker compose build
        docker compose up -d
        echo
        echo "Done! Run './scripts/status.sh' to check health."
        exit 0
    fi
fi

# Prompt for required values
echo "Enter the hostname or IP of your CloudKey / UNVR:"
read -rp "  PROTECT_HOST: " protect_host
if [ -z "$protect_host" ]; then
    echo "ERROR: PROTECT_HOST is required." >&2
    exit 1
fi

echo
echo "Enter the host path for the archive directory:"
echo "  (This is where .mp4 files will be stored on the NAS)"
read -rp "  ARCHIVE_PATH: " archive_path
if [ -z "$archive_path" ]; then
    echo "ERROR: ARCHIVE_PATH is required." >&2
    exit 1
fi

echo
echo "Enter your timezone (e.g. America/Chicago, UTC):"
read -rp "  TZ [UTC]: " tz
tz="${tz:-UTC}"

echo
echo "Enter the cron schedule for backups:"
read -rp "  CRON_SCHEDULE [0 * * * *]: " cron_schedule
cron_schedule="${cron_schedule:-0 * * * *}"

# Generate .env
cp .env.example .env
sed -i "s|^PROTECT_HOST=.*|PROTECT_HOST=${protect_host}|" .env
sed -i "s|^ARCHIVE_PATH=.*|ARCHIVE_PATH=${archive_path}|" .env
sed -i "s|^TZ=.*|TZ=${tz}|" .env
sed -i "s|^CRON_SCHEDULE=.*|CRON_SCHEDULE=${cron_schedule}|" .env

echo
echo "Generated .env:"
grep -E '^[A-Z]' .env
echo

# Create archive directory
if [ ! -d "$archive_path" ]; then
    echo "Creating archive directory: ${archive_path}"
    mkdir -p "$archive_path"
fi

# Test SSH
echo "Testing SSH to ${protect_host}..."
ssh_key_path="${SSH_KEY_PATH:-$HOME/.ssh}"
if ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes "root@${protect_host}" "echo ok" &>/dev/null; then
    echo "SSH connection OK"
else
    echo "WARNING: Cannot SSH to root@${protect_host}"
    echo "Make sure your SSH key is authorized on the CloudKey before starting."
    echo
fi

# Build and start
echo
echo "Building Docker image..."
docker compose build

echo
echo "Starting container..."
docker compose up -d

echo
echo "=== Installation complete ==="
echo "  Logs:   docker compose logs -f"
echo "  Status: ./scripts/status.sh"
echo "  Update: ./scripts/update.sh"
