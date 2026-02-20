# unvr-nas-backup

Dockerized backup system that pulls surveillance video from a Unifi Protect CloudKey/UNVR, remuxes `.ubv` files to `.mp4`, renames them with camera names, and archives them to a NAS.

## How it works

```
NAS (Docker)                          CloudKey / UNVR
┌────────────────────────┐            ┌─────────────────────┐
│  cron → backup.sh      │──── SSH ──▶│  PostgreSQL :5433    │
│                        │            │  .ubv video files    │
│  1. Query DB for files │            └─────────────────────┘
│  2. SCP .ubv to staging│
│  3. Remux → .mp4       │
│  4. Rename with camera │
│     name + timestamps  │
│  5. Archive to         │
│     /CamName/date/     │
└────────────────────────┘
```

## Quick start

```bash
# Clone to NAS
git clone https://github.com/YOUR_USER/unvr-nas-backup.git /opt/unvr-nas-backup
cd /opt/unvr-nas-backup

# Configure
cp .env.example .env
# Edit .env — set PROTECT_HOST and ARCHIVE_PATH at minimum

# Create archive directory
mkdir -p /path/to/your/archive

# Start
docker compose up -d

# Watch logs
docker compose logs -f
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PROTECT_HOST` | *(required)* | Hostname or IP of the CloudKey / UNVR |
| `PROTECT_SSH_USER` | `root` | SSH user on the CloudKey |
| `PROTECT_SSH_PORT` | `22` | SSH port on the CloudKey |
| `PROTECT_VIDEO_PATH` | `/srv/unifi-protect/video` | Video file path on the CloudKey |
| `PROTECT_DB_PORT` | `5433` | PostgreSQL port on the CloudKey |
| `PROTECT_DB_NAME` | `unifi-protect` | PostgreSQL database name |
| `BACKUP_HOURS` | `1` | How many hours back to look for recordings |
| `BATCH_SIZE` | `5` | Number of files to SCP before pausing |
| `BATCH_DELAY` | `30` | Seconds to pause between SCP batches |
| `ARCHIVE_PATH` | *(required)* | Host path for the archive volume mount |
| `SSH_KEY_PATH` | `~/.ssh` | Host path to SSH keys (mounted read-only) |
| `CRON_SCHEDULE` | `0 * * * *` | Cron expression for backup frequency |
| `RUN_ON_START` | `true` | Run a backup immediately on container start |
| `TZ` | `UTC` | Timezone |
| `LOG_LEVEL` | `info` | Log level: `debug`, `info`, `warn`, `error` |

## Prerequisites

- Docker and Docker Compose on the NAS
- SSH key-based access from the NAS to the CloudKey (`ssh root@<cloudkey>` must work without a password)
- The CloudKey must be running Unifi Protect with PostgreSQL on port 5433

## Archive structure

```
/archive/
├── Front-Door/
│   ├── 2026-02-19/
│   │   ├── Front-Door_2026-02-19_08-00-00_to_08-05-00.mp4
│   │   └── Front-Door_2026-02-19_08-05-00_to_08-10-00.mp4
│   └── 2026-02-20/
│       └── ...
├── Backyard/
│   └── ...
└── ...
```

## Verification

```bash
# Check remux binary
docker compose run --rm unvr-nas-backup remux --version

# Test SSH connectivity
docker compose run --rm unvr-nas-backup ssh -o StrictHostKeyChecking=accept-new root@<cloudkey> echo ok

# Run a one-off backup
docker compose run --rm -e RUN_ON_START=true unvr-nas-backup

# Check cron is running
docker compose exec unvr-nas-backup pgrep -x cron
```

## Troubleshooting

- **SSH fails**: Ensure your SSH key is in `SSH_KEY_PATH` and is authorized on the CloudKey. The container copies keys to fix permissions automatically.
- **No recordings found**: Increase `BACKUP_HOURS` or check that the CloudKey has active recordings. Only `type=rotating` and `active=false` files are selected.
- **Remux fails**: Verify the `.ubv` file is complete (not still being recorded). The query filters `active=false` to prevent this.
- **Disk space**: Monitor `/staging` (named volume) and `/archive`. Staging is cleaned after each run.

## License

MIT
