<p align="center">
  <img src="docs/images/ozark-connect-logo.png" alt="Ozark Connect" width="200">
</p>

# unvr-nas-backup

[![GitHub Release](https://img.shields.io/github/v/release/Ozark-Connect/unvr-nas-backup)](https://github.com/Ozark-Connect/unvr-nas-backup/releases)
[![GitHub last commit](https://img.shields.io/github/last-commit/Ozark-Connect/unvr-nas-backup)](https://github.com/Ozark-Connect/unvr-nas-backup/commits)
[![GitHub Stars](https://img.shields.io/github/stars/Ozark-Connect/unvr-nas-backup)](https://github.com/Ozark-Connect/unvr-nas-backup/stargazers)
[![License](https://img.shields.io/badge/license-MIT-green)](https://github.com/Ozark-Connect/unvr-nas-backup/blob/main/LICENSE)

Dockerized backup system that pulls continuous surveillance video from a UniFi Protect device (CloudKey, UCG, UDM, UNVR, etc.), remuxes `.ubv` files to `.mp4`, renames them with camera names, and archives them to a NAS.

> **Note:** Recordings can only be backed up after the Protect device finishes writing them, so there is an inherent delay (typically one recording segment, ~1 GB per file). This is **not** real-time replication - it is near-real-time archival. For the best coverage, pair this tool with UniFi Protect's built-in **Continuous Archiving** (UI Labs) feature, which handles detection event clips. As far as we know, this is the only open-source tool that backs up continuous recording video.

## How it works

```
NAS (Docker)                        Protect Device (CloudKey, UCG, UDM, UNVR, etc.)
┌────────────────────────┐          ┌─────────────────────────┐
│  cron → backup.sh      │── SSH -> │  PostgreSQL :5433       │
│                        │          │  .ubv video files       │
│  1. Query DB for files │          └─────────────────────────┘
│  2. SCP .ubv to staging│
│  3. Remux → .mp4       │
│  4. Rename with camera │
│     name + timestamps  │
│  5. Archive to         │
│     /CamName/date/     │
└────────────────────────┘
```

## Prerequisites

- Docker and Docker Compose on the NAS (x86_64 or ARM64)
- SSH key-based access from the NAS to the Protect device (`ssh root@<host>` must work without a password)
- UniFi Protect running with PostgreSQL on port 5433

## Quick start

```bash
# Clone to NAS
git clone https://github.com/Ozark-Connect/unvr-nas-backup.git /opt/unvr-nas-backup
cd /opt/unvr-nas-backup

# Interactive install - prompts for host and archive path, pulls image and starts
./install.sh

# Or manually:
cp .env.example .env
# Edit .env - set PROTECT_HOST and ARCHIVE_PATH at minimum
docker compose up -d
```

The pre-built image is pulled automatically from `ghcr.io/ozark-connect/unvr-nas-backup:latest`. To build locally instead, run `docker compose build`.

## Helper scripts

| Script | Description |
|---|---|
| `./install.sh` | First-time setup - creates `.env`, tests SSH, pulls image and starts the container |
| `./scripts/status.sh` | Shows backup health: last run, archive stats, disk usage, container/cron status |
| `./scripts/run-now.sh` | Triggers an immediate backup without waiting for cron |
| `./scripts/update.sh` | Pulls latest code and image, and restarts the container |

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PROTECT_HOST` | *(required)* | Hostname or IP of your Protect device |
| `PROTECT_SSH_USER` | `root` | SSH user (`root` on most devices, may differ on standalone UNVRs) |
| `PROTECT_VIDEO_PATH` | `/srv/unifi-protect/video` | Fallback video path if the DB-reported folder is not accessible |
| `PROTECT_DB_PORT` | `5433` | PostgreSQL port |
| `PROTECT_DB_NAME` | `unifi-protect` | PostgreSQL database name |
| `BACKUP_HOURS` | `1` | How many hours back to look for recordings. Set this to at least 2x your cron interval so recordings that finish between runs are not missed. |
| `BATCH_SIZE` | `5` | Number of files to SCP before pausing |
| `BATCH_DELAY` | `30` | Seconds to pause between SCP batches |
| `ARCHIVE_PATH` | *(required)* | Host path for the archive volume mount |
| `SSH_KEY_PATH` | `~/.ssh` | Host path to SSH keys (mounted read-only) |
| `CRON_SCHEDULE` | `*/15 * * * *` | Cron expression for backup frequency (do not quote this value) |
| `RUN_ON_START` | `true` | Run a backup immediately on container start |
| `TZ` | `UTC` | Timezone for log messages. Archive filenames always use UTC regardless of this setting. |
| `LOG_LEVEL` | `info` | Log level: `debug`, `info`, `warn`, `error` |

> **Disk usage**: Continuous recording generates roughly 10-20 GB per camera per day depending on resolution and scene activity. Plan your archive storage accordingly.

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

> **Note:** The `by-date` directory uses symlinks, which may not be visible when browsing via SMB/Windows shares. The `by-camera` directory always works directly.

## Troubleshooting

- **SSH fails**: Ensure your SSH key is in `SSH_KEY_PATH` and is authorized on the Protect device. The container copies keys to fix permissions automatically.
- **No recordings found**: Increase `BACKUP_HOURS` or check that the device has active recordings. Only `type=rotating` and `active=false` files are selected.
- **Remux fails**: Verify the `.ubv` file is complete (not still being recorded). The query filters `active=false` to prevent this.
- **Disk space**: Monitor `/staging` (named volume) and `/archive`. Staging is cleaned after each run. The backup script warns when archive space drops below 1 GB.

## Acknowledgments

This project uses [unifi-protect-remux](https://github.com/petergeneric/unifi-protect-remux) by Peter Wright for converting `.ubv` video files to `.mp4`. The remux binary is licensed under [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html) and is downloaded at build time - see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for details.

## Also from Ozark Connect

If you find this useful, check out **[Network Optimizer](https://github.com/Ozark-Connect/NetworkOptimizer)** - a self-hosted UniFi network analysis platform with security auditing, Wi-Fi optimization, LAN speed testing with Layer 2 path tracing, adaptive SQM, coverage mapping, and more.

## Sponsor

If you find this project useful, consider [sponsoring the maintainer](https://github.com/sponsors/tvancott42).

## License

MIT - see [LICENSE](LICENSE) for details.

---

<sub>unvr-nas-backup is an independent project by Ozark Connect and is not affiliated with, endorsed by, or sponsored by Ubiquiti, Inc. Ubiquiti, UniFi, UniFi Protect, UCG, UDM, UDR, UNVR, and Cloud Key are trademarks or registered trademarks of Ubiquiti, Inc. All other trademarks are the property of their respective owners.</sub>
