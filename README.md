<p align="center">
  <img src="docs/images/ozark-connect-logo.png" alt="Ozark Connect" width="200">
</p>

# unvr-nas-backup

[![GitHub Release](https://img.shields.io/github/v/release/Ozark-Connect/unvr-nas-backup)](https://github.com/Ozark-Connect/unvr-nas-backup/releases)
[![GitHub last commit](https://img.shields.io/github/last-commit/Ozark-Connect/unvr-nas-backup)](https://github.com/Ozark-Connect/unvr-nas-backup/commits)
[![GitHub Stars](https://img.shields.io/github/stars/Ozark-Connect/unvr-nas-backup)](https://github.com/Ozark-Connect/unvr-nas-backup/stargazers)
[![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/Ozark-Connect/unvr-nas-backup/blob/main/LICENSE)

Dockerized backup system that pulls continuous surveillance video from a UniFi Protect device (CloudKey, UCG, UDM, UNVR, etc.), remuxes `.ubv` files to `.mp4`, renames them with camera IDs and timestamps, and archives them to a NAS.

> **Note:** Recordings can only be backed up after the Protect device finishes writing them. UniFi Protect writes ~1 GB `.ubv` segments; busy cameras close segments quickly, but **low-activity cameras may take many hours** to fill a segment, so there can be a significant delay before those recordings appear in the archive. This is **not** real-time replication - it is near-real-time archival. For the best coverage, pair this tool with UniFi Protect's built-in **Continuous Archiving** (UI Labs) feature, which handles detection event clips. As far as we know, this is the only open-source tool that backs up continuous recording video.

## How it works

```
NAS (Docker)                        Protect Device (CloudKey, UCG, UDM, UNVR, etc.)
┌────────────────────────┐          ┌─────────────────────────┐
│  cron → backup.sh      │── SSH -> │  PostgreSQL :5433       │
│                        │          │  .ubv video files       │
│  1. Query DB for files │          └─────────────────────────┘
│  2. SCP .ubv to staging│
│  3. Remux → .mp4       │          Amazon S3 (optional)
│  4. Rename with camera │          ┌─────────────────────────┐
│     ID + timestamps    │── S3 --> │  by-id/CamID/       │
│  5. Archive to         │          │    date/*.mp4           │
│     /CamID/date/       │          └─────────────────────────┘
│  6. Upload to S3       │
│  7. (optional) Delete  │
│     local files        │
└────────────────────────┘
```

## Prerequisites

- Docker and Docker Compose on the NAS (x86_64 or ARM64)
- Network connectivity from the NAS to the Protect device
- SSH enabled on the Protect device (the installer can set up key auth for you)
- A UniFi Protect device with active recordings

### Setting up SSH access

1. **Enable SSH on your Protect device**: In your UniFi Console, go to **Settings -> Control Plane -> Console -> SSH** and enable it. Note the password you set here. The default SSH username is root for Console devices.

2. **Run the installer** - it will detect that SSH key auth isn't set up and offer to configure it automatically. It generates a key if needed and copies it to your Protect device using `ssh-copy-id`. You just need to enter the SSH password once.

   If you prefer to set up SSH manually:
   ```bash
   ssh-keygen -t ed25519                  # generate a key (if you don't have one)
   ssh-copy-id root@<protect-host>        # copy it to the Protect device
   ssh root@<protect-host>                # verify it works (no password prompt)
   ```

The container mounts your key from `SSH_KEY_PATH` (defaults to `~/.ssh`) and uses it automatically.

## Quick start

**Option 1** - Standalone install (no git required):
```bash
mkdir -p /opt/unvr-nas-backup && cd /opt/unvr-nas-backup
curl -fsSLO https://raw.githubusercontent.com/Ozark-Connect/unvr-nas-backup/main/install.sh
chmod +x install.sh && ./install.sh
```

**Option 2** - Clone the repo (includes helper scripts for status, updates, etc.):
```bash
git clone https://github.com/Ozark-Connect/unvr-nas-backup.git /opt/unvr-nas-backup
cd /opt/unvr-nas-backup
./install.sh
```

The installer prompts for your Protect device host, archive path, and other settings. It downloads `compose.yml` if needed, pulls the pre-built image from GHCR, and starts the container.

**Manual setup** (no installer):
```bash
mkdir -p /opt/unvr-nas-backup && cd /opt/unvr-nas-backup
curl -fsSLO https://raw.githubusercontent.com/Ozark-Connect/unvr-nas-backup/main/compose.yml
curl -fsSLO https://raw.githubusercontent.com/Ozark-Connect/unvr-nas-backup/main/.env.example
cp .env.example .env
# Edit .env - set PROTECT_HOST and ARCHIVE_PATH at minimum
docker compose up -d
```

## Helper scripts

| Script | Description |
|---|---|
| `./install.sh` | First-time setup - creates `.env`, tests SSH, pulls image and starts the container |
| `./scripts/status.sh` | Shows backup health: last run, archive stats, disk usage, container/cron status |
| `./scripts/run-now.sh` | Triggers an immediate backup without waiting for cron |
| `./scripts/update.sh` | Pulls latest code and image, and restarts the container |

## Updating

If you cloned the repo:
```bash
cd /opt/unvr-nas-backup
./scripts/update.sh
```

Standalone install:
```bash
cd /opt/unvr-nas-backup
docker compose pull && docker compose up -d
```

Your `.env` and archive are preserved in both cases.

## Stopping

```bash
cd /opt/unvr-nas-backup
docker compose down
```

This stops the container. Your archive and `.env` are not affected. Run `docker compose up -d` to start it again.

## Uninstalling

```bash
cd /opt/unvr-nas-backup
docker compose down -v              # stop container and remove staging volume
docker rmi ghcr.io/ozark-connect/unvr-nas-backup:latest   # remove the image
cd / && rm -rf /opt/unvr-nas-backup # remove install directory
```

Your archive directory is **not** deleted automatically. Remove it manually if you no longer need the recordings.

## Status API

The container includes a lightweight JSON API for monitoring backup health and browsing the archive. It is enabled by default on port `7550`.

```bash
# Backup health and configuration
curl http://<nas-ip>:7550/api/status

# List all archived recordings with local/S3 location
curl http://<nas-ip>:7550/api/backups

# Trigger a backup (uses system defaults)
curl -X POST http://<nas-ip>:7550/api/backup

# Trigger a backup for a specific camera and time range
curl -X POST http://<nas-ip>:7550/api/backup \
  -H "Content-Type: application/json" \
  -d '{"camera_id": "63a1f2bcde0400038f000123", "start": 1744000000000, "end": 1744034400000}'

# Simple health check
curl http://<nas-ip>:7550/api/health

# List cameras in the index
curl http://<nas-ip>:7550/api/cameras

# Add a camera to the index
curl -X POST http://<nas-ip>:7550/api/cameras \
  -H "Content-Type: application/json" \
  -d '{"camera_id": "63a1f2bcde0400038f000123", "name": "Front Door"}'

# Disable a camera (stop backing it up)
curl -X PUT http://<nas-ip>:7550/api/cameras \
  -H "Content-Type: application/json" \
  -d '{"camera_id": "63a1f2bcde0400038f000123", "enabled": false}'

# Remove a camera from the index
curl -X DELETE "http://<nas-ip>:7550/api/cameras?camera_id=63a1f2bcde0400038f000123"
```

### `GET /api/status`

Returns the current backup state, last success time, archive disk usage, cron schedule, and S3 configuration.

```json
{
  "status": "idle",
  "last_success_epoch": 1744034400,
  "last_success_age_seconds": 482,
  "cron_schedule": "*/15 * * * *",
  "archive": {
    "total_bytes": 536870912000,
    "used_bytes": 128849018880,
    "free_bytes": 408021893120,
    "used_percent": 24.0
  },
  "s3": {
    "enabled": true,
    "bucket": "my-protect-backups",
    "prefix": "unifi-protect",
    "region": "us-east-1",
    "storage_class": "STANDARD_IA",
    "delete_local": false
  },
  "camera_index": {
    "cameras": [
      {"id": "63a1f2bcde0400038f000123", "name": "Front Door", "enabled": true}
    ],
    "total": 1,
    "enabled": 1
  }
}
```

The `status` field is `"running"` when a backup is in progress and `"idle"` otherwise. `last_success_epoch` is `null` until the first successful backup completes. The `camera_index` section summarizes the camera allow-list (see [Camera index](#camera-index) below).

### `GET /api/backups`

Lists every archived recording grouped by camera, with `local` and `s3` booleans indicating where each file exists. When S3 is enabled, the endpoint queries S3 once to build a full inventory.

```json
{
  "cameras": {
    "63a1f2bcde0400038f000123": [
      {
        "file": "63a1f2bcde0400038f000123_2026-02-19_08-00-00_to_08-05-00.mp4",
        "path": "63a1f2bcde0400038f000123/2026-02-19/63a1f2bcde0400038f000123_2026-02-19_08-00-00_to_08-05-00.mp4",
        "size_bytes": 104857600,
        "local": true,
        "s3": true
      }
    ],
    "74b2e3cdef0500049g111234": [
      {
        "file": "74b2e3cdef0500049g111234_2026-02-19_08-00-00_to_08-10-00.mp4",
        "path": "74b2e3cdef0500049g111234/2026-02-19/74b2e3cdef0500049g111234_2026-02-19_08-00-00_to_08-10-00.mp4",
        "size_bytes": null,
        "local": false,
        "s3": true
      }
    ]
  },
  "total_recordings": 150,
  "total_local": 120,
  "total_s3": 150
}
```

Files where `local` is `false` and `s3` is `true` were uploaded to S3 and then deleted locally (via `S3_DELETE_LOCAL`). `size_bytes` is `null` for S3-only files.

> **Note:** For large archives the `/api/backups` endpoint may take several seconds to respond, especially when S3 inventory is being queried.

### `GET /api/playback`

Given a camera ID and time range, returns the list of video files needed for continuous playback in the correct order, along with seek offsets into the first and last files to cover exactly the requested range.

All three query parameters are required:

| Parameter | Type | Description |
|---|---|---|
| `camera_id` | string | Protect database ID of the camera |
| `start` | integer | Start of the range in epoch milliseconds |
| `end` | integer | End of the range in epoch milliseconds |

```bash
curl "http://<nas-ip>:7550/api/playback?camera_id=63a1f2bcde0400038f000123&start=1744000000000&end=1744003600000"
```

Response:

```json
{
  "camera_id": "63a1f2bcde0400038f000123",
  "range": {
    "start_ms": 1744000000000,
    "end_ms": 1744003600000
  },
  "files": [
    {
      "file": "63a1f2bcde0400038f000123_2026-04-07_08-00-00_to_08-05-00.mp4",
      "path": "63a1f2bcde0400038f000123/2026-04-07/63a1f2bcde0400038f000123_2026-04-07_08-00-00_to_08-05-00.mp4",
      "recording_start_ms": 1743998400000,
      "recording_end_ms": 1744000700000
    },
    {
      "file": "63a1f2bcde0400038f000123_2026-04-07_08-05-00_to_08-10-00.mp4",
      "path": "63a1f2bcde0400038f000123/2026-04-07/63a1f2bcde0400038f000123_2026-04-07_08-05-00_to_08-10-00.mp4",
      "recording_start_ms": 1744000700000,
      "recording_end_ms": 1744003000000
    }
  ],
  "playback": {
    "start_offset_ms": 1600000,
    "end_offset_ms": 2300000,
    "start_offset_seconds": 1600.0,
    "end_offset_seconds": 2300.0,
    "total_files": 2
  }
}
```

The `files` array is sorted in chronological order. Play them sequentially, seeking to `start_offset_seconds` in the first file and stopping at `end_offset_seconds` in the last file. Files in between should be played in full.

Returns `404` if the camera ID doesn't exist in the archive or no recordings overlap the requested range.

### `POST /api/backup`

Triggers a backup run. The backup starts asynchronously and the endpoint returns immediately with a `202 Accepted`. If a backup is already running, returns `409 Conflict`.

All parameters are optional. When omitted, the system defaults from `.env` are used (`BACKUP_HOURS` for time range, all cameras).

| Field | Type | Description |
|---|---|---|
| `camera_id` | string | Protect database ID of a single camera to back up |
| `start` | number | Start of time range in epoch milliseconds. Recordings that end after this time are included. |
| `end` | number | End of time range in epoch milliseconds. Recordings that start before this time are included. |

```bash
# Back up everything from the last hour (system defaults)
curl -X POST http://<nas-ip>:7550/api/backup

# Back up a specific camera only
curl -X POST http://<nas-ip>:7550/api/backup \
  -H "Content-Type: application/json" \
  -d '{"camera_id": "63a1f2bcde0400038f000123"}'

# Back up a specific 6-hour window
curl -X POST http://<nas-ip>:7550/api/backup \
  -H "Content-Type: application/json" \
  -d '{"start": 1744000000000, "end": 1744021600000}'
```

Response (`202 Accepted`):

```json
{
  "triggered": true,
  "params": {
    "camera_id": "63a1f2bcde0400038f000123"
  }
}
```

When no overrides are provided, `params` is `"defaults"`. Use `GET /api/status` to check whether the triggered backup is still running.

### Camera index

The camera index (`/archive/_index.json`) controls which cameras are included in scheduled backup runs. When the `cameras` array is empty (or the file doesn't exist), **all cameras** are backed up — this is the backwards-compatible default.

When cameras are added to the index, only cameras with `"enabled": true` are included in scheduled backups. API-triggered backups that specify an explicit `camera_id` bypass the index entirely.

The index file is created automatically on first container start and persists in the archive volume.

### `GET /api/cameras`

Returns the current camera index.

```json
{
  "cameras": [
    {"id": "63a1f2bcde0400038f000123", "name": "Front Door", "enabled": true},
    {"id": "74b2e3cdef0500049g111234", "name": "Garage", "enabled": false}
  ],
  "total": 2,
  "enabled": 1
}
```

Cameras with `"enabled": false` are in the index but excluded from scheduled backups.

### `POST /api/cameras`

Adds a camera to the index.

| Field | Type | Required | Description |
|---|---|---|---|
| `camera_id` | string | yes | Protect database ID of the camera |
| `name` | string | no | Human-readable label (informational only) |
| `enabled` | boolean | no | Whether to include in scheduled backups (default `true`) |

```bash
curl -X POST http://<nas-ip>:7550/api/cameras \
  -H "Content-Type: application/json" \
  -d '{"camera_id": "63a1f2bcde0400038f000123", "name": "Front Door"}'
```

Response (`201 Created`):

```json
{"id": "63a1f2bcde0400038f000123", "name": "Front Door", "enabled": true}
```

Returns `409 Conflict` if the camera already exists in the index.

### `PUT /api/cameras`

Updates an existing camera in the index (e.g. to enable/disable it or change its name).

| Field | Type | Required | Description |
|---|---|---|---|
| `camera_id` | string | yes | Protect database ID of the camera |
| `name` | string | no | Updated name |
| `enabled` | boolean | no | Updated enabled state |

```bash
# Disable a camera (stop backing it up)
curl -X PUT http://<nas-ip>:7550/api/cameras \
  -H "Content-Type: application/json" \
  -d '{"camera_id": "63a1f2bcde0400038f000123", "enabled": false}'
```

Response (`200 OK`):

```json
{"id": "63a1f2bcde0400038f000123", "name": "Front Door", "enabled": false}
```

Returns `404` if the camera is not in the index.

### `DELETE /api/cameras`

Removes a camera from the index. Accepts the camera ID as a query parameter or in a JSON body.

```bash
# Via query parameter
curl -X DELETE "http://<nas-ip>:7550/api/cameras?camera_id=63a1f2bcde0400038f000123"

# Via JSON body
curl -X DELETE http://<nas-ip>:7550/api/cameras \
  -H "Content-Type: application/json" \
  -d '{"camera_id": "63a1f2bcde0400038f000123"}'
```

Response (`200 OK`):

```json
{"removed": "63a1f2bcde0400038f000123"}
```

Returns `404` if the camera is not in the index. Removing all cameras from the index reverts to backing up all cameras.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PROTECT_HOST` | *(required)* | Hostname or IP of your Protect device |
| `PROTECT_SSH_USER` | `root` | SSH user (`root` on most devices, may differ on standalone UNVRs) |
| `PROTECT_VIDEO_PATH` | `/srv/unifi-protect/video` | Fallback video path if the DB-reported folder is not accessible |
| `PROTECT_DB_PORT` | `5433` | PostgreSQL port |
| `PROTECT_DB_NAME` | `unifi-protect` | PostgreSQL database name |
| `BACKUP_HOURS` | `1` | How many hours back to look for completed recordings (based on end time). Set this to at least 2x your cron interval so recordings that finish between runs are not missed. |
| `BACKUP_CHANNELS` | `0` | Which recording channels to back up (comma-separated). `0` = main high-res stream, `2` = low-quality sub-stream. Most users only need `0`. |
| `BATCH_SIZE` | `5` | Number of files to SCP before pausing |
| `BATCH_DELAY` | `30` | Seconds to pause between SCP batches |
| `ARCHIVE_PATH` | *(required)* | Host path for the archive volume mount |
| `SSH_KEY_PATH` | `~/.ssh` | Host path to SSH keys (mounted read-only) |
| `CRON_SCHEDULE` | `*/15 * * * *` | Cron expression for backup frequency (do not quote this value) |
| `RUN_ON_START` | `true` | Run a backup immediately on container start |
| `TZ` | `UTC` | Timezone for log messages. Archive filenames always use UTC regardless of this setting. |
| `LOG_LEVEL` | `info` | Log level: `debug`, `info`, `warn`, `error` |
| `API_ENABLED` | `true` | Enable the built-in status API. Set to `false` to disable. |
| `API_PORT` | `7550` | Port the status API listens on |
| `RETENTION_DAYS` | *(disabled)* | Delete footage older than N days. Runs after each backup. |
| `RETENTION_PERCENT` | *(disabled)* | When disk usage exceeds N%, prune oldest footage until it drops below. Runs after each backup. |

> **Disk usage**: Continuous recording generates roughly 10-20 GB per camera per day depending on resolution and scene activity. Plan your archive storage accordingly.
>
> **Retention**: Both retention settings are disabled by default (the archive grows indefinitely). When both are set, age-based pruning (`RETENTION_DAYS`) runs first, then disk-based pruning (`RETENTION_PERCENT`) kicks in if usage is still over the threshold. Pruning always deletes the oldest dates first.

### Amazon S3 upload

After each backup run, archived `.mp4` files can be automatically uploaded to an S3 bucket. S3 upload is disabled by default.

| Variable | Default | Description |
|---|---|---|
| `S3_ENABLED` | `false` | Set to `true` to enable S3 uploads |
| `S3_BUCKET` | *(required when enabled)* | S3 bucket name |
| `S3_PREFIX` | *(none)* | Optional key prefix (folder) inside the bucket. No leading or trailing slash. |
| `S3_REGION` | `us-east-1` | AWS region for the bucket |
| `S3_STORAGE_CLASS` | `STANDARD` | S3 storage class. Options: `STANDARD`, `STANDARD_IA`, `ONEZONE_IA`, `INTELLIGENT_TIERING`, `GLACIER`, `DEEP_ARCHIVE`, `GLACIER_IR` |
| `S3_ENDPOINT_URL` | *(none)* | Custom endpoint URL for S3-compatible services (MinIO, Backblaze B2, Wasabi, etc.) |
| `AWS_ACCESS_KEY_ID` | *(none)* | AWS access key (or mount `~/.aws` — see below) |
| `AWS_SECRET_ACCESS_KEY` | *(none)* | AWS secret key |
| `S3_DELETE_LOCAL` | `false` | Delete local `.mp4` files after they are confirmed uploaded to S3. See warning below. |

**Providing AWS credentials** — you have two options:

1. **Environment variables** (simpler): Set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in your `.env` file.
2. **AWS config mount** (supports profiles, SSO, etc.): Uncomment the `~/.aws` volume mount in `compose.yml`. The container will read `~/.aws/credentials` and `~/.aws/config` from the host.

If running on an EC2 instance with an IAM instance profile, neither is needed — the AWS CLI picks up the role automatically.

**S3 file structure** — files are uploaded under the same `by-id/` structure used locally:

```
s3://my-protect-backups/
└── unifi-protect/                     ← S3_PREFIX (optional)
    └── by-id/
        ├── 63a1f2bcde0400038f000123/
        │   └── 2026-02-19/
        │       ├── 63a1f2bcde0400038f000123_2026-02-19_08-00-00_to_08-05-00.mp4
        │       └── ...
        └── 74b2e3cdef0500049g111234/
            └── ...
```

**Sync strategy** — uploads use `aws s3 sync` with `max_concurrent_requests` set to 20 for parallel transfers. Only new or changed files (compared by size) are uploaded. Symlinks are excluded automatically.

**Upload verification** — when `S3_DELETE_LOCAL` is enabled, each newly archived file is verified after the sync by comparing the file size reported by S3 against the local file. If verification fails, the local file is kept and a warning is logged.

> **Warning — `S3_DELETE_LOCAL`**: When set to `true`, local `.mp4` files are deleted immediately after their S3 upload is verified. Once deleted, the **only** copy of those recordings lives in S3. Make sure your S3 bucket has versioning or cross-region replication configured if data durability is critical. Empty directories and stale by-date symlinks are cleaned up automatically.

**S3-compatible storage** — any service that implements the S3 API can be used by setting `S3_ENDPOINT_URL`. Tested configurations include MinIO and Backblaze B2. Example for Backblaze B2:

```bash
S3_ENABLED=true
S3_BUCKET=my-b2-bucket
S3_ENDPOINT_URL=https://s3.us-west-000.backblazeb2.com
S3_REGION=us-west-000
AWS_ACCESS_KEY_ID=<your-b2-key-id>
AWS_SECRET_ACCESS_KEY=<your-b2-application-key>
```

## Compatibility

Tested and confirmed working on:

| Device | Status |
|---|---|
| UniFi CloudKey Gen2+ | Tested |
| UniFi Cloud Gateway (UCG-Fiber, UCG-Ultra, UCG-Max, etc.) | Tested |
| UniFi Dream Machine (UDM, UDM-Pro, UDM-SE, etc.) | Likely compatible - testers welcome |
| UniFi Dream Router (UDR, etc.) | Likely compatible - testers welcome |
| UniFi NVR (UNVR, UNVR-Pro, etc.) | Likely compatible - testers welcome |

Should work on any device running UniFi Protect with SSH access, PostgreSQL on port 5433, and `.ubv` video files at `/srv/unifi-protect/video`. If you've tested on a device not listed here, please [open an issue](https://github.com/Ozark-Connect/unvr-nas-backup/issues) to let us know.

## Archive structure

Archive directories and filenames use the camera's Protect database ID rather than its display name. This ensures stable, rename-safe paths regardless of how cameras are named in the Protect UI.

```
/archive/
├── by-id/                          # Canonical storage (by camera ID)
│   ├── 63a1f2bcde0400038f000123/
│   │   ├── 2026-02-19/
│   │   │   ├── 63a1f2bcde0400038f000123_2026-02-19_08-00-00_to_08-05-00.mp4
│   │   │   └── 63a1f2bcde0400038f000123_2026-02-19_08-05-00_to_08-10-00.mp4
│   │   ├── 2026-02-20/
│   │   │   └── ...
│   │   └── low-quality/                # Only if BACKUP_CHANNELS includes 2
│   │       └── 2026-02-19/
│   │           └── 63a1f2bcde0400038f000123_2026-02-19_00-00-00_to_10-00-00_low-quality.mp4
│   └── 74b2e3cdef0500049g111234/
│       └── ...
└── by-date/                            # Symlinks
    ├── 2026-02-19/
    │   ├── 63a1f2bcde0400038f000123             → ../../by-id/63a1f2bcde0400038f000123/2026-02-19
    │   ├── 63a1f2bcde0400038f000123-low-quality → ../../by-id/63a1f2bcde0400038f000123/low-quality/2026-02-19
    │   └── 74b2e3cdef0500049g111234             → ../../by-id/74b2e3cdef0500049g111234/2026-02-19
    └── 2026-02-20/
        └── ...
```

> **Note:** The `by-date` directory uses symlinks, which may not be visible when browsing via SMB/Windows shares. The `by-id` directory always works directly.

## Troubleshooting

- **SSH fails**: Ensure your SSH key is in `SSH_KEY_PATH` and is authorized on the Protect device. The container copies keys to fix permissions automatically.
- **No recordings found**: Increase `BACKUP_HOURS` or check that the device has active recordings. Only `type=rotating` and `active=false` files are selected.
- **Remux fails**: Verify the `.ubv` file is complete (not still being recorded). The query filters `active=false` to prevent this.
- **Container shows unhealthy**: This is normal until the first successful backup completes. With `RUN_ON_START=true` (the default), this resolves within a few minutes of starting.
- **Disk space**: The backup script logs two levels of disk space warnings that you can use to set up alerts (e.g., Docker log monitoring, webhook, etc.):
  - `[backup] ... WARN: Archive volume has less than 100 GB free` - time to plan cleanup
  - `[backup] ... WARN: CRITICAL: Archive volume has less than 10 GB free` - backups may start failing

  There is no automatic pruning yet (see [Future features](#future-features)). You are responsible for managing retention (e.g., deleting old date folders, NAS-level quotas, or a cron job). Staging is cleaned after each run.
- **Permissions**: The installer needs Docker access and write permissions to the archive path - on most NAS systems you're already root. The container runs as root internally for cron, SSH key handling, and volume writes. It does not expose any ports or accept inbound connections.

## Performance

The backup process has negligible impact on the Protect device. We profiled a CloudKey Gen2+ while backups ran every minute and the device sat at 70-90% CPU idle throughout. The database query finishes so fast it doesn't even register at 10-second sampling intervals, and the SCP file transfer (the heaviest part) briefly uses one core for SSH encryption before dropping back to baseline. Memory, swap, and PostgreSQL activity are unchanged during backups.

The project defaults to AES-128-GCM for SSH encryption, which takes advantage of hardware AES extensions present on CloudKey Gen2+, UCG-Fiber, and likely all current UniFi Protect devices. Peak CPU during transfers is about the same either way (one core doing cipher work), but AES-GCM finishes ~27% faster - so the device spends less total time under load per backup cycle.

See [docs/performance.md](docs/performance.md) for the full profiling data, cipher benchmarks, and methodology.

## Future features

- **Archive pruning** - Automatically delete old recordings from the NAS archive based on configurable retention policies (per-camera, per-channel, minimum free space triggers, dry-run mode).
- **Protect device cleanup** - After recordings are safely backed up, optionally remove them from the Protect device (both `.ubv` files and DB rows) to free up space. Off by default, with safety checks and configurable minimum age before cleanup.

See [TODO.md](TODO.md) for details.

## Acknowledgments

This project uses [unifi-protect-remux](https://github.com/petergeneric/unifi-protect-remux) by Peter Wright for converting `.ubv` video files to `.mp4`. The remux binary is licensed under [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html) and is downloaded at build time - see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for details.

## Related projects

**[unifi-protect-backup](https://github.com/ep1cman/unifi-protect-backup)** by ep1cman is an excellent tool for backing up UniFi Protect event clips in real time via the Protect API. It captures motion events, smart detections (person, vehicle, animal, etc.), and supports rclone backends so you can archive directly to cloud storage. It's well maintained and worth checking out.

The two tools are complementary. ep1cman's tool handles **events**: the clips Protect generates when it detects something. This tool handles the **continuous 24/7 recordings between events** that the Protect API doesn't expose. Together they give you full off-box coverage of everything your cameras record.

Why does that matter? Event-based backup has an inherent blind spot: if Protect's detection doesn't fire (common at night, in low contrast conditions, with unusual angles, or just edge cases where the AI doesn't trigger), that footage never gets exported. It's still on the Protect device's drive, but it's not in your backup. This tool pulls the complete recording history directly from the device regardless of whether an event was generated, so nothing falls through the cracks.

## Also from Ozark Connect

<p align="center">
  <a href="https://github.com/Ozark-Connect/NetworkOptimizer"><img src="https://github.com/Ozark-Connect/NetworkOptimizer/raw/main/docs/images/app-logo-v2.png" alt="Network Optimizer" width="200"></a>
</p>

If you find this useful, check out **[Network Optimizer](https://github.com/Ozark-Connect/NetworkOptimizer)** - a self-hosted UniFi network analysis platform with security auditing, Wi-Fi optimization, LAN speed testing with Layer 2 path tracing, adaptive SQM, coverage mapping, and more.

## Sponsor

If you find this project useful, consider [sponsoring the maintainer](https://github.com/sponsors/tvancott42).

## License

MIT - see [LICENSE](LICENSE) for details.

---

<sub>unvr-nas-backup is an independent project by Ozark Connect and is not affiliated with, endorsed by, or sponsored by Ubiquiti, Inc. Ubiquiti, UniFi, UniFi Protect, UCG, UDM, UDR, UNVR, and Cloud Key are trademarks or registered trademarks of Ubiquiti, Inc. All other trademarks are the property of their respective owners.</sub>
