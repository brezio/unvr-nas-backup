# unvr-nas-backup

Dockerized backup system that pulls continuous surveillance video from a UniFi Protect device (UNVR, CloudKey, UCG, UDM, etc.), remuxes `.ubv` files to `.mp4`, and archives them to a NAS with optional S3 sync.

## Architecture

The system runs as three Docker services defined in `compose.yml`:

- **exporter** — Runs cron (PID 1) which triggers `backup.sh` on a configurable interval. Handles SSH/SCP from the Protect device, `.ubv` → `.mp4` remuxing, archiving to `/archive`, and optional S3 sync. Also runs a lightweight internal trigger server (`trigger.py`) so the API can request on-demand backups.
- **api** — Python stdlib HTTP API (`api.py`) that serves all client-facing endpoints. Manages the backup queue, camera index, playback lookups, and status. Triggers backups by calling the exporter's internal HTTP endpoint.
- **nginx** — Reverse proxy that exposes the API on port 80.

The exporter and API share two volumes: `/archive` (the recording archive, read-write for exporter, read-only for API) and `/shared` (lock file and last-success timestamp). Both containers read the same `.env` file.

Camera selection is controlled by `/archive/_index.json`. When its `cameras` array is empty or the file is missing, all cameras are backed up. When populated, only cameras with `"enabled": true` are included in scheduled runs. API-triggered backups with an explicit `camera_id` bypass the index.

Ubiquiti Protect devices do not store timezone in their database. On startup, the exporter queries the device's system timezone via `timedatectl` over SSH and caches it to `/shared/timezone`. The API fetches this cached value from the exporter's `GET /unvr/timezone` endpoint on first use. Individual cameras can still override the timezone via the camera index (`_index.json`), but the device timezone is used as the default fallback.

## Key files

- `exporter/backup.sh` — main backup pipeline (query DB → SCP `.ubv` → remux → archive → S3 sync)
- `exporter/entrypoint.sh` — exporter container init (SSH setup, index creation, trigger server, optional first backup, cron)
- `exporter/trigger.py` — internal HTTP server that accepts backup trigger requests from the API and serves UNVR metadata (cameras, ranges, timezone)
- `exporter/Dockerfile` — Debian bookworm-slim with python3, AWS CLI v2, unifi-protect-remux, cron
- `api/api.py` — stdlib-only HTTP API server (no pip dependencies), all endpoints below
- `api/Dockerfile` — Debian bookworm-slim with python3, AWS CLI v2 (for S3 inventory queries)
- `compose.yml` — Docker Compose multi-service definition
- `nginx.conf` — nginx reverse proxy configuration
- `install.sh` — interactive installer that generates `.env` and starts the stack

## Archive structure

```
/archive/
├── _index.json                         # Camera allow-list and sync metadata
├── by-id/                              # Canonical storage (camera ID directories)
│   └── {camera_id}/
│       └── {YYYY-MM-DD}/
│           └── {camera_id}_{date}_{HH-MM-SS}_to_{HH-MM-SS}.mp4
└── by-date/                            # Symlinks into by-id/ (convenience view)
```

## API reference

Base URL: `http://<host>:{API_PORT}` (default port `7550`). All responses are JSON. All timestamps are epoch milliseconds unless noted.

### GET /api/health

Returns `{"ok": true}`. Use for liveness probes.

### GET /api/status

System overview: backup state (`idle` or `running`), last success time, cron schedule, archive disk usage, S3 config, camera index summary, and last sync timestamp.

Response shape:
```json
{
  "status": "idle|running",
  "last_success_epoch": 1744034400,
  "last_success_age_seconds": 482,
  "cron_schedule": "*/15 * * * *",
  "archive": { "total_bytes": int, "used_bytes": int, "free_bytes": int, "used_percent": float },
  "s3": { "enabled": bool, "bucket": str|null, "prefix": str|null, "region": str, "storage_class": str, "delete_local": bool },
  "camera_index": { "cameras": [...], "total": int, "enabled": int },
  "last_synced": int|null,
  "queue": { "queue_size": int, "backup_running": bool, "items": [...] }
}
```

### GET /api/backups

All archived recordings grouped by camera ID with `local` and `s3` booleans. Queries S3 inventory when enabled (may be slow for large archives).

Response shape:
```json
{
  "cameras": { "<camera_id>": [{ "file": str, "path": str, "size_bytes": int|null, "local": bool, "s3": bool }] },
  "total_recordings": int,
  "total_local": int,
  "total_s3": int
}
```

### GET /api/playback

Given a camera and time range, returns the ordered list of `.mp4` files needed for continuous playback with seek offsets into the first and last file.

Query parameters (all required):

| Param | Type | Description |
|---|---|---|
| `camera_id` | string | Camera ID |
| `start` | integer | Range start (epoch ms) |
| `end` | integer | Range end (epoch ms) |

Example: `GET /api/playback?camera_id=abc123&start=1744000000000&end=1744003600000`

Response shape:
```json
{
  "camera_id": str,
  "range": { "start_ms": int, "end_ms": int },
  "files": [{ "file": str, "path": str, "recording_start_ms": int, "recording_end_ms": int }],
  "playback": {
    "start_offset_ms": int,
    "end_offset_ms": int,
    "start_offset_seconds": float,
    "end_offset_seconds": float,
    "total_files": int
  }
}
```

Error codes: `400` missing/invalid params, `404` camera not found or no overlapping recordings.

### POST /api/backup

Triggers a backup. If the system is idle, starts immediately. If a backup is already running or the queue is non-empty, the request is queued and executed in order. Returns `202 Accepted` in both cases.

Duplicate requests (same `camera_id`/`start`/`end` already queued) are deduplicated — the existing request is returned with `"duplicate": true`. If multiple callers submit the same request with different `callback_url` values, all callbacks are fired when the request completes.

Request body (JSON, all fields optional):

| Field | Type | Description |
|---|---|---|
| `camera_id` | string | Limit to a single camera |
| `start` | number | Range start (epoch ms) |
| `end` | number | Range end (epoch ms) |
| `callback_url` | string | URL to POST with the result when the request finishes |

When `camera_id`, `start`, and `end` are all omitted, system defaults are used (`BACKUP_HOURS` lookback, all cameras subject to the index).

Response shape (immediate):
```json
{
  "request_id": str,
  "triggered": true,
  "queued": false,
  "params": { ... } | "defaults"
}
```

Response shape (queued):
```json
{
  "request_id": str,
  "status": "queued",
  "queued": true,
  "duplicate": false,
  "position": int,
  "params": { ... } | "defaults"
}
```

Callback payload (POSTed to `callback_url` on completion):
```json
{
  "request_id": str,
  "status": "completed" | "failed",
  "result": str,
  "params": { ... } | "defaults",
  "started_at": float,
  "finished_at": float,
  "duration_seconds": float
}
```

### GET /api/backup/{request_id}

Look up the status of a backup request by its ID.

Response shape:
```json
{
  "request_id": str,
  "status": "queued" | "running" | "completed" | "failed",
  "params": { ... } | "defaults",
  "created_at": float,
  "started_at": float | null,
  "finished_at": float | null,
  "result": str | null,
  "position": int,
  "duration_seconds": float
}
```

`position` is only present when the request is still queued. `duration_seconds` is only present when the request has finished. Finished requests are retained for 1 hour after completion.

Error codes: `404` if the request ID is not found or has expired.

### GET /api/queue

Inspect the current backup queue size, running state, and estimated wait times.

Response shape:
```json
{
  "queue_size": int,
  "backup_running": bool,
  "average_backup_duration_seconds": float,
  "items": [{
    "request_id": str,
    "position": int,
    "params": { ... } | "defaults",
    "created_at": float,
    "estimated_wait_seconds": float
  }]
}
```

`average_backup_duration_seconds` and per-item `estimated_wait_seconds` are only present when there is historical duration data to base estimates on.

### GET /api/cameras

Returns all cameras with date-range availability from the local archive, S3, and the UNVR. Accepts an optional `camera_id` query parameter to return a single camera.

Response shape (list):
```json
{
  "cameras": [{
    "id": str, "name": str, "timezone": str|null, "enabled": bool,
    "archive": { "oldest_ms": int, "newest_ms": int, "recording_count": int } | null,
    "s3": { "oldest_ms": int, "newest_ms": int, "recording_count": int } | null,
    "unvr": { "oldest_ms": int, "newest_ms": int, "recording_count": int } | null
  }],
  "total": int,
  "enabled": int,
  "unvr_error": str | absent
}
```

Response shape (single camera via `?camera_id=<id>`):
```json
{
  "id": str, "name": str, "timezone": str|null, "enabled": bool,
  "archive": { ... } | null,
  "s3": { ... } | null,
  "unvr": { ... } | null,
  "unvr_error": str | absent
}
```

- `archive` — local `.mp4` date range (from filenames). `null` if no local recordings.
- `s3` — S3 date range. `null` if S3 is disabled or no S3 recordings.
- `unvr` — full UNVR recording range (SSH/psql query). `null` if query fails.
- `unvr_error` — included when the SSH connection to the UNVR fails.

Returns `404` when `camera_id` is provided but not found in the index.

### DELETE /api/cameras/{camera_id}

Removes a camera from the index. Returns `404` if not found.

Example: `DELETE /api/cameras/abc123`

Response: `{ "removed": "abc123" }`

### POST /api/cameras

Adds a camera to the index. Returns `201 Created` or `409 Conflict` if it already exists.

Request body (JSON):

| Field | Type | Required | Description |
|---|---|---|---|
| `camera_id` | string | yes | Protect database camera ID |
| `name` | string | no | Human-readable label |
| `enabled` | boolean | no | Include in scheduled backups (default `true`) |
| `timezone` | string | no | IANA timezone (e.g. `America/Chicago`) |

Response: the created camera object.

### PATCH /api/cameras/{camera_id}

Updates an existing camera. Only provided fields are changed — omitted fields remain as-is. Returns `404` if not found.

Request body (JSON):

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | no | New name |
| `enabled` | boolean | no | New enabled state |
| `timezone` | string | no | New IANA timezone |

At least one of `name`, `enabled`, or `timezone` is required. Response: the updated camera object.

### GET /api/cameras/sync

Dry run. SSHs into the UNVR, queries the `cameras` table, and compares against the index. Returns proposed changes without modifying anything. Returns `502` if the SSH/DB query fails.

Sync rules:
- Camera on UNVR but not in config → `add` with `enabled: false`
- Camera in config but not on UNVR (and currently enabled) → `disable`
- Name differs between UNVR and config → `update_name`

Response shape:
```json
{
  "unvr_cameras": int,
  "changes": [{ "action": "add|disable|update_name", "camera_id": str, "reason": str, ... }],
  "total_changes": int
}
```

### POST /api/cameras/sync

Same comparison as GET, but applies all changes to `_index.json` and updates the root `last_synced` timestamp. No request body needed.

Response shape:
```json
{
  "synced": true,
  "changes_applied": int,
  "changes": [...],
  "cameras": [...],
  "last_synced": int
}
```

## _index.json schema

```json
{
  "cameras": [
    { "id": "hex_camera_id", "name": "Human Name", "timezone": "America/Chicago", "enabled": true }
  ],
  "last_synced": 1744034400
}
```

- Empty `cameras` array = back up all cameras (backwards-compatible default)
- `last_synced` is set by `POST /api/cameras/sync` (epoch seconds, not ms)

## Environment variables

Both containers read from the shared `.env` file. The API also receives `EXPORTER_URL` via the compose `environment` key.

| Variable | Default | Used by |
|---|---|---|
| `PROTECT_HOST` | *(required)* | exporter, api (sync) |
| `PROTECT_SSH_USER` | `root` | exporter, api (sync) |
| `PROTECT_DB_PORT` | `5433` | exporter, api (sync) |
| `PROTECT_DB_NAME` | `unifi-protect` | exporter, api (sync) |
| `SSH_OPTS` | *(set by entrypoint)* | exporter, api (sync) |
| `API_PORT` | `7550` | api |
| `EXPORTER_PORT` | `8550` | exporter (trigger server) |
| `EXPORTER_URL` | `http://exporter:8550` | api (set via compose) |
| `NGINX_PORT` | `7550` | nginx (host port) |
| `BACKUP_HOURS` | `1` | exporter |
| `BATCH_SIZE` | `20` | exporter |
| `BATCH_DELAY` | `5` | exporter |
| `REMUX_JOBS` | `4` | exporter |
| `CRON_SCHEDULE` | `*/15 * * * *` | exporter |
| `S3_ENABLED` | `false` | exporter, api |
| `S3_BUCKET` | *(required when S3 enabled)* | exporter, api |
| `S3_PREFIX` | *(none)* | exporter, api |
| `S3_REGION` | `us-east-1` | exporter, api |
| `S3_STORAGE_CLASS` | `STANDARD` | exporter |
| `S3_ENDPOINT_URL` | *(none)* | exporter, api |
| `S3_DELETE_LOCAL` | `false` | exporter, api |

## Conventions

- Camera IDs are hex strings from the Protect database (`cameras.id`), typically 24 characters.
- All API timestamps are **epoch milliseconds** except `last_synced` which is epoch seconds.
- Recording filenames: `{camera_id}_{YYYY-MM-DD}_{HH-MM-SS}_to_{HH-MM-SS}.mp4` (UTC). Low-quality channels add a `_low-quality` suffix before `.mp4`.
- The API uses only Python stdlib (`http.server`, `json`, `subprocess`, etc.) — no pip dependencies.
- S3 uploads use `aws s3 sync` with `max_concurrent_requests=20` and `--size-only` comparison.
- Backup locking uses `flock` on `/tmp/backup.lock`. The API probes this lock non-destructively to report status.
