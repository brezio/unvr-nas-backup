# unvr-nas-backup

Dockerized backup system that pulls continuous surveillance video from a UniFi Protect device (CloudKey, UCG, UDM, UNVR, etc.), remuxes `.ubv` files to `.mp4`, renames them with camera names, and archives them to a NAS.

> **Note:** This project backs up *continuous recording video*, not detection event clips (which UniFi Protect can already export natively via the Protect Labs feature). As far as we know, this is the only open-source tool that does this.

## How it works

```
NAS (Docker)                       Protect Device (CloudKey, UCG, UDM, UNVR, etc.)
┌────────────────────────┐         ┌─────────────────────┐
│  cron → backup.sh      │── SSH ─▶│  PostgreSQL :5433    │
│                        │         │  .ubv video files    │
│  1. Query DB for files │         └─────────────────────┘
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
git clone https://github.com/Ozark-Connect/unvr-nas-backup.git /opt/unvr-nas-backup
cd /opt/unvr-nas-backup

# Interactive install — prompts for host and archive path, builds and starts
./install.sh

# Or manually:
cp .env.example .env
# Edit .env — set PROTECT_HOST and ARCHIVE_PATH at minimum
docker compose up -d
```

The pre-built image is pulled automatically from `ghcr.io/ozark-connect/unvr-nas-backup:latest`. To build locally instead, run `docker compose build`.

## Helper scripts

| Script | Description |
|---|---|
| `./install.sh` | First-time setup — creates `.env`, tests SSH, builds and starts the container |
| `./scripts/status.sh` | Shows backup health: last run, archive stats, disk usage, container/cron status |
| `./scripts/run-now.sh` | Triggers an immediate backup without waiting for cron |
| `./scripts/update.sh` | Pulls latest code, rebuilds the image, and restarts the container |

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PROTECT_HOST` | *(required)* | Hostname or IP of your Protect device |
| `PROTECT_SSH_USER` | `root` | SSH user |
| `PROTECT_SSH_PORT` | `22` | SSH port |
| `PROTECT_VIDEO_PATH` | `/srv/unifi-protect/video` | Video file path on the device |
| `PROTECT_DB_PORT` | `5433` | PostgreSQL port |
| `PROTECT_DB_NAME` | `unifi-protect` | PostgreSQL database name |
| `BACKUP_HOURS` | `1` | How many hours back to look for recordings |
| `BATCH_SIZE` | `5` | Number of files to SCP before pausing |
| `BATCH_DELAY` | `30` | Seconds to pause between SCP batches |
| `ARCHIVE_PATH` | *(required)* | Host path for the archive volume mount |
| `SSH_KEY_PATH` | `~/.ssh` | Host path to SSH keys (mounted read-only) |
| `CRON_SCHEDULE` | `*/15 * * * *` | Cron expression for backup frequency |
| `RUN_ON_START` | `true` | Run a backup immediately on container start |
| `TZ` | `UTC` | Timezone |
| `LOG_LEVEL` | `info` | Log level: `debug`, `info`, `warn`, `error` |

## Compatibility

Tested and confirmed working on:

| Device | Status |
|---|---|
| UniFi CloudKey Gen2+ | Tested |
| UniFi Cloud Gateway (UCG-Fiber, UCG-Ultra, UCG-Max, etc.) | Tested |
| UniFi Dream Machine (UDM, UDM-Pro, UDM-SE, etc.) | Likely compatible — testers welcome |
| UniFi Dream Router (UDR, etc.) | Likely compatible — testers welcome |
| UniFi NVR (UNVR, UNVR-Pro, etc.) | Likely compatible — testers welcome |

Should work on any device running UniFi Protect with SSH access, PostgreSQL on port 5433, and `.ubv` video files at `/srv/unifi-protect/video`. If you've tested on a device not listed here, please [open an issue](https://github.com/Ozark-Connect/unvr-nas-backup/issues) to let us know.

## Prerequisites

- Docker and Docker Compose on the NAS
- SSH key-based access from the NAS to the Protect device (`ssh root@<host>` must work without a password)
- UniFi Protect running with PostgreSQL on port 5433

## Archive structure

Files are stored canonically by camera, with date-based symlinks for browsing by date:

```
/archive/
├── by-camera/                          # Canonical storage
│   ├── Front-Door/
│   │   ├── 2026-02-19/
│   │   │   ├── Front-Door_2026-02-19_08-00-00_to_08-05-00.mp4
│   │   │   └── Front-Door_2026-02-19_08-05-00_to_08-10-00.mp4
│   │   └── 2026-02-20/
│   │       └── ...
│   └── Backyard/
│       └── ...
└── by-date/                            # Symlinks
    ├── 2026-02-19/
    │   ├── Front-Door → ../../by-camera/Front-Door/2026-02-19
    │   └── Backyard   → ../../by-camera/Backyard/2026-02-19
    └── 2026-02-20/
        └── ...
```

## Troubleshooting

- **SSH fails**: Ensure your SSH key is in `SSH_KEY_PATH` and is authorized on the Protect device. The container copies keys to fix permissions automatically.
- **No recordings found**: Increase `BACKUP_HOURS` or check that the device has active recordings. Only `type=rotating` and `active=false` files are selected.
- **Remux fails**: Verify the `.ubv` file is complete (not still being recorded). The query filters `active=false` to prevent this.
- **Disk space**: Monitor `/staging` (named volume) and `/archive`. Staging is cleaned after each run.

## Acknowledgments

This project uses [unifi-protect-remux](https://github.com/petergeneric/unifi-protect-remux) by Peter Wright for converting `.ubv` video files to `.mp4`. The remux binary is licensed under [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html) and is downloaded at build time — see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for details.

## Sponsor

If you find this project useful, consider [sponsoring the maintainer](https://github.com/sponsors/tvancott42).

## License

MIT — see [LICENSE](LICENSE) for details.

---

<sub>unvr-nas-backup is an independent project by Ozark Connect and is not affiliated with, endorsed by, or sponsored by Ubiquiti, Inc. Ubiquiti, UniFi, UniFi Protect, UCG, UDM, UDR, UNVR, and Cloud Key are trademarks or registered trademarks of Ubiquiti, Inc. All other trademarks are the property of their respective owners.</sub>
