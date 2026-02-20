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
        echo "Keeping existing .env. Pulling latest image and starting..."
        docker compose pull
        docker compose up -d
        echo
        echo "Done! Run './scripts/status.sh' to check health."
        exit 0
    fi
fi

# Prompt for required values
echo "Enter the hostname or IP of your Protect device (CloudKey, UCG, UDM, UNVR, etc.):"
read -rp "  PROTECT_HOST: " protect_host
if [ -z "$protect_host" ]; then
    echo "ERROR: PROTECT_HOST is required." >&2
    exit 1
fi

echo
echo "Enter the SSH user for your Protect device (root for most devices):"
read -rp "  PROTECT_SSH_USER [root]: " ssh_user
ssh_user="${ssh_user:-root}"

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
read -rp "  CRON_SCHEDULE [*/15 * * * *]: " cron_schedule
cron_schedule="${cron_schedule:-*/15 * * * *}"

# Generate .env
cat > .env <<ENVEOF
# Required - hostname or IP of your Protect device (CloudKey, UCG, UDM, UNVR, etc.)
PROTECT_HOST=${protect_host}

# SSH user (root for most devices, may differ on standalone UNVRs)
PROTECT_SSH_USER=${ssh_user}

# PostgreSQL settings
PROTECT_DB_PORT=5433
PROTECT_DB_NAME=unifi-protect

# How many hours back to look for recordings
BACKUP_HOURS=1

# SCP batching - copy N files, then pause BATCH_DELAY seconds
BATCH_SIZE=5
BATCH_DELAY=30

# Required - host path where archived .mp4 files are stored
ARCHIVE_PATH=${archive_path}

# Host path to SSH keys (must contain a key authorized on the Protect device)
SSH_KEY_PATH=~/.ssh

# Cron schedule (default: every 15 minutes)
# Do not quote this value - Docker Compose includes quotes literally
CRON_SCHEDULE=${cron_schedule}

# Run a backup immediately on container start
RUN_ON_START=true

# Timezone
TZ=${tz}

# Log level: debug, info, warn, error
LOG_LEVEL=info
ENVEOF

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
echo "Testing SSH to ${ssh_user}@${protect_host}..."
ssh_key_path="${SSH_KEY_PATH:-$HOME/.ssh}"
if ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -i "$(ls "${ssh_key_path}"/id_ed25519 "${ssh_key_path}"/id_rsa "${ssh_key_path}"/id_ecdsa 2>/dev/null | head -1)" "${ssh_user}@${protect_host}" "echo ok" &>/dev/null; then
    echo "SSH connection OK"
else
    echo "WARNING: Cannot SSH to ${ssh_user}@${protect_host}"
    echo "Make sure your SSH key in ${ssh_key_path} is authorized on the Protect device before starting."
    echo
fi

# Pull and start
echo
echo "Pulling Docker image..."
docker compose pull

echo
echo "Starting container..."
docker compose up -d

echo
echo "=== Installation complete ==="
echo "  Logs:   docker compose logs -f"
echo "  Status: ./scripts/status.sh"
echo "  Update: ./scripts/update.sh"
