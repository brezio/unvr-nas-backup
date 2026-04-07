#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

REPO_URL="https://raw.githubusercontent.com/Ozark-Connect/unvr-nas-backup/main"

echo "=== unvr-nas-backup installer ==="
echo

# Download compose.yml if not present (standalone install)
if [ ! -f compose.yml ]; then
    echo "Downloading compose.yml..."
    curl -fsSL "${REPO_URL}/compose.yml" -o compose.yml
fi

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
        if [ -f ./scripts/status.sh ]; then
            echo "Done! Run './scripts/status.sh' to check health."
        else
            echo "Done! Run 'docker compose logs' to check output."
        fi
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
echo "  Examples: */15 * * * * (every 15 min), */30 * * * * (every 30 min), 0 * * * * (hourly)"
read -rp "  CRON_SCHEDULE [*/15 * * * *]: " cron_schedule
cron_schedule="${cron_schedule:-*/15 * * * *}"

# ── S3 configuration (optional) ─────────────────────────────────────────────
echo
read -rp "Enable Amazon S3 uploads? [y/N] " enable_s3
s3_enabled="false"
s3_bucket=""
s3_prefix=""
s3_region="us-east-1"
s3_storage_class="STANDARD"
s3_endpoint_url=""
aws_access_key_id=""
aws_secret_access_key=""
s3_delete_local="false"

if [[ "$enable_s3" =~ ^[Yy] ]]; then
    s3_enabled="true"
    echo
    echo "Enter the S3 bucket name:"
    read -rp "  S3_BUCKET: " s3_bucket
    if [ -z "$s3_bucket" ]; then
        echo "ERROR: S3_BUCKET is required when S3 is enabled." >&2
        exit 1
    fi

    echo
    echo "Enter an optional key prefix (folder) inside the bucket."
    echo "  Leave blank for none. No leading or trailing slash."
    read -rp "  S3_PREFIX []: " s3_prefix

    echo
    echo "Enter the AWS region for the bucket:"
    read -rp "  S3_REGION [us-east-1]: " s3_region_input
    s3_region="${s3_region_input:-us-east-1}"

    echo
    echo "Enter the S3 storage class:"
    echo "  Options: STANDARD, STANDARD_IA, ONEZONE_IA, INTELLIGENT_TIERING,"
    echo "           GLACIER, DEEP_ARCHIVE, GLACIER_IR"
    read -rp "  S3_STORAGE_CLASS [STANDARD]: " s3_storage_class_input
    s3_storage_class="${s3_storage_class_input:-STANDARD}"

    echo
    echo "Using an S3-compatible service (MinIO, Backblaze B2, Wasabi, etc.)?"
    echo "  Enter the endpoint URL, or leave blank for AWS S3."
    read -rp "  S3_ENDPOINT_URL []: " s3_endpoint_url

    echo
    echo "AWS credentials — you can provide an access key pair here, or skip"
    echo "and mount ~/.aws into the container instead (see compose.yml)."
    echo "If running on EC2 with an IAM role, skip this."
    read -rp "  AWS_ACCESS_KEY_ID []: " aws_access_key_id
    if [ -n "$aws_access_key_id" ]; then
        read -rsp "  AWS_SECRET_ACCESS_KEY: " aws_secret_access_key
        echo
        if [ -z "$aws_secret_access_key" ]; then
            echo "ERROR: AWS_SECRET_ACCESS_KEY is required when AWS_ACCESS_KEY_ID is set." >&2
            exit 1
        fi
    fi

    echo
    echo "Delete local .mp4 files after they are confirmed uploaded to S3?"
    echo "  WARNING: Once deleted, the only copy lives in S3."
    read -rp "  S3_DELETE_LOCAL [y/N]: " s3_delete_local_input
    if [[ "$s3_delete_local_input" =~ ^[Yy] ]]; then
        s3_delete_local="true"
    fi
fi

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

# Append S3 configuration if enabled
if [ "$s3_enabled" = "true" ]; then
    cat >> .env <<S3EOF

# ── Amazon S3 upload ─────────────────────────────────────────────────────────
S3_ENABLED=true
S3_BUCKET=${s3_bucket}
S3EOF

    [ -n "$s3_prefix" ] && echo "S3_PREFIX=${s3_prefix}" >> .env
    echo "S3_REGION=${s3_region}" >> .env
    echo "S3_STORAGE_CLASS=${s3_storage_class}" >> .env
    [ -n "$s3_endpoint_url" ] && echo "S3_ENDPOINT_URL=${s3_endpoint_url}" >> .env
    [ -n "$aws_access_key_id" ] && echo "AWS_ACCESS_KEY_ID=${aws_access_key_id}" >> .env
    [ -n "$aws_secret_access_key" ] && echo "AWS_SECRET_ACCESS_KEY=${aws_secret_access_key}" >> .env
    echo "S3_DELETE_LOCAL=${s3_delete_local}" >> .env
fi

echo
echo "Generated .env:"
grep -E '^[A-Z]' .env | sed 's/^\(AWS_SECRET_ACCESS_KEY=\).*/\1********/'
echo

# Create archive directory
if [ ! -d "$archive_path" ]; then
    echo "Creating archive directory: ${archive_path}"
    mkdir -p "$archive_path"
fi

# Test SSH
echo "Testing SSH to ${ssh_user}@${protect_host}..."
ssh_key_path="${SSH_KEY_PATH:-$HOME/.ssh}"
ssh_key_file="$(ls "${ssh_key_path}"/id_ed25519 "${ssh_key_path}"/id_rsa "${ssh_key_path}"/id_ecdsa 2>/dev/null | head -1 || true)"

ssh_ok=false
if [ -n "$ssh_key_file" ]; then
    if ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -i "$ssh_key_file" "${ssh_user}@${protect_host}" "echo ok" &>/dev/null; then
        ssh_ok=true
    fi
fi

if $ssh_ok; then
    echo "SSH connection OK"
else
    echo "SSH key auth is not working yet."
    echo
    if ! command -v ssh-copy-id &>/dev/null; then
        echo "WARNING: ssh-copy-id is not installed on this system."
        echo "Install openssh-client or set up SSH key auth manually (see README)."
        echo
    else
        read -rp "Set up SSH key auth now? You'll need the SSH password from your UniFi Console. [Y/n] " setup_ssh
        if [[ ! "$setup_ssh" =~ ^[Nn] ]]; then
            # Generate key if none exists
            if [ -z "$ssh_key_file" ]; then
                echo "No SSH key found. Generating one..."
                mkdir -p "${ssh_key_path}"
                ssh-keygen -t ed25519 -N "" -f "${ssh_key_path}/id_ed25519"
                ssh_key_file="${ssh_key_path}/id_ed25519"
                echo
            fi

            # Copy key to Protect device
            echo "Copying SSH key to ${ssh_user}@${protect_host}..."
            echo "Enter the SSH password when prompted."
            if ssh-copy-id -i "$ssh_key_file" "${ssh_user}@${protect_host}"; then
                echo
                # Verify key-based auth works
                echo "Verifying key-based SSH..."
                if ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -i "$ssh_key_file" "${ssh_user}@${protect_host}" "echo ok" &>/dev/null; then
                    echo "SSH key auth is working."
                else
                    echo "WARNING: SSH key was copied but verification failed."
                    echo "You may need to troubleshoot SSH access before backups will work."
                    echo
                fi
            else
                echo
                echo "WARNING: ssh-copy-id failed. Check the password and try again, or set up SSH manually."
                echo "See the README for manual SSH setup instructions."
                echo
            fi
        else
            echo "Skipping SSH key setup."
            echo "Make sure your SSH key in ${ssh_key_path} is authorized on the Protect device before starting."
            echo
        fi
    fi
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
echo "  API:    http://localhost:7550/api/status"
if [ -f ./scripts/status.sh ]; then
    echo "  Status: ./scripts/status.sh"
fi
if [ -f ./scripts/update.sh ]; then
    echo "  Update: ./scripts/update.sh"
else
    echo "  Update: docker compose pull && docker compose up -d"
fi
