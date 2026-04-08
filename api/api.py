#!/usr/bin/env python3
"""API service for unvr-nas-backup.

Runs as a standalone container. Uses only the Python standard library so no pip
dependencies are required. Reads the archive and shared volumes, and triggers
backups via the exporter service's internal HTTP endpoint.

Endpoints:
    GET    /api/status              — current backup health and configuration
    GET    /api/backups             — list all archived recordings with local/S3 location
    GET    /api/playback            — files needed to play back a camera's footage for a time range
    POST   /api/backup              — trigger or queue a backup (returns request_id)
    GET    /api/backup/{request_id} — look up the status of a backup request
    GET    /api/queue               — inspect the current backup queue
    GET    /api/cameras             — list cameras in the index
    GET    /api/cameras/{id}        — get a single camera
    POST   /api/cameras             — add a camera to the index
    PATCH  /api/cameras/{id}        — update a camera in the index
    DELETE /api/cameras/{id}        — remove a camera from the index
    GET    /api/cameras/sync        — preview changes from UNVR (dry run)
    POST   /api/cameras/sync        — sync camera index with UNVR and apply changes
"""

import calendar
import json
import os
import re
import subprocess
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

# ── Configuration ───────────────────────────────────────────────────────────

ARCHIVE_DIR = Path("/archive")
LOCKFILE = Path("/shared/backup.lock")
LAST_SUCCESS_FILE = Path("/shared/backup-last-success")

# Internal exporter service URL — the exporter exposes a trigger endpoint
EXPORTER_URL = os.environ.get("EXPORTER_URL", "http://exporter:8550")

API_PORT = int(os.environ.get("API_PORT", "7550"))

S3_ENABLED = os.environ.get("S3_ENABLED", "false") == "true"
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_PREFIX = os.environ.get("S3_PREFIX", "")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "")
S3_DELETE_LOCAL = os.environ.get("S3_DELETE_LOCAL", "false") == "true"
S3_STORAGE_CLASS = os.environ.get("S3_STORAGE_CLASS", "STANDARD")

CRON_SCHEDULE = os.environ.get("CRON_SCHEDULE", "*/15 * * * *")

INDEX_FILE = ARCHIVE_DIR / "_index.json"

# ── Device timezone (fetched from exporter on first use) ────────────────────
_device_tz_cache = {"value": None}


def _get_device_timezone():
    """Fetch the Protect device's timezone from the exporter, with caching."""
    if _device_tz_cache["value"] is not None:
        return _device_tz_cache["value"]

    try:
        url = f"{EXPORTER_URL}/unvr/timezone"
        resp = urlopen(Request(url), timeout=5)
        data = json.loads(resp.read())
        tz = data.get("timezone", "UTC")
    except Exception:
        tz = "UTC"

    _device_tz_cache["value"] = tz
    return tz



# ── Backup Queue ───────────────────────────────────────────────────────────
#
# In-memory queue for backup requests. When a backup is already running,
# new requests are queued and executed in order once the current backup
# finishes. Identical requests (same camera_id/start/end) are deduplicated.
# Each request gets a UUID for status tracking and supports an optional
# callback URL that is POSTed to when the request completes.

_queue_lock = threading.Lock()
_queue: list[dict] = []           # ordered list of pending requests
_requests: dict[str, dict] = {}   # request_id → full request state
_dedup_set: set[tuple] = set()    # (camera_id, start, end) tuples currently queued

# Request states: queued → running → completed | failed
# Completed/failed requests are kept for lookup but pruned after 1 hour.

_REQUEST_TTL = 3600  # seconds to keep finished requests


def _dedup_key(camera_id, start, end):
    """Return the deduplication key for a backup request."""
    return (camera_id or "", start or 0, end or 0)


def _prune_finished_requests():
    """Remove completed/failed requests older than _REQUEST_TTL."""
    now = time.time()
    expired = [
        rid for rid, req in _requests.items()
        if req["status"] in ("completed", "failed")
        and now - req.get("finished_at", now) > _REQUEST_TTL
    ]
    for rid in expired:
        _requests.pop(rid, None)


def _enqueue_backup(camera_id=None, start=None, end=None, callback_url=None):
    """Add a backup request to the queue.

    Returns (request_dict, error_string). On success the request dict
    contains the assigned ``request_id`` and current ``status``.
    Duplicate requests (same camera_id/start/end already queued) return
    the existing request rather than creating a new entry.
    """
    key = _dedup_key(camera_id, start, end)

    with _queue_lock:
        _prune_finished_requests()

        # Dedup: if an identical request is already queued, return it
        if key in _dedup_set:
            for req in _queue:
                if req["_dedup_key"] == key and req["status"] == "queued":
                    # Merge callback URL if new request brings one
                    if callback_url and callback_url not in req.get("callback_urls", []):
                        req.setdefault("callback_urls", []).append(callback_url)
                    return {
                        "request_id": req["request_id"],
                        "status": req["status"],
                        "queued": True,
                        "duplicate": True,
                        "position": _queue.index(req) + 1,
                        "params": req["params"],
                    }, None

        request_id = str(uuid.uuid4())
        params = {}
        if camera_id:
            params["camera_id"] = camera_id
        if start is not None:
            params["start"] = int(start)
        if end is not None:
            params["end"] = int(end)

        req = {
            "request_id": request_id,
            "status": "queued",
            "params": params if params else "defaults",
            "callback_urls": [callback_url] if callback_url else [],
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "_dedup_key": key,
            "_camera_id": camera_id,
            "_start": start,
            "_end": end,
        }

        _queue.append(req)
        _requests[request_id] = req
        _dedup_set.add(key)

        position = len(_queue)

    return {
        "request_id": request_id,
        "status": "queued",
        "queued": True,
        "duplicate": False,
        "position": position,
        "params": req["params"],
    }, None


def _fire_callbacks(req, payload):
    """POST the result payload to each callback URL (best-effort)."""
    for url in req.get("callback_urls", []):
        try:
            data = json.dumps(payload).encode()
            http_req = Request(url, data=data, method="POST")
            http_req.add_header("Content-Type", "application/json")
            urlopen(http_req, timeout=10)
        except Exception:
            pass  # best-effort — don't let callback failures block the queue


def _process_queue():
    """Background thread: waits for idle state then runs the next queued backup.

    Polls every 5 seconds. When the system is idle and the queue is
    non-empty, pops the next request and executes it synchronously
    (from this thread's perspective). After the backup finishes,
    fires any registered callback URLs.
    """
    while True:
        time.sleep(5)

        with _queue_lock:
            if not _queue:
                continue
            if _backup_is_running():
                continue

            # Pop the next request
            req = _queue.pop(0)
            key = req["_dedup_key"]
            _dedup_set.discard(key)
            req["status"] = "running"
            req["started_at"] = time.time()

        # Trigger the backup via the exporter's internal HTTP endpoint
        camera_id = req["_camera_id"]
        start = req["_start"]
        end = req["_end"]

        trigger_body = {}
        if camera_id:
            trigger_body["camera_id"] = str(camera_id)
        if start is not None:
            trigger_body["start"] = int(start)
        if end is not None:
            trigger_body["end"] = int(end)

        try:
            data = json.dumps(trigger_body).encode()
            http_req = Request(
                f"{EXPORTER_URL}/trigger",
                data=data, method="POST",
            )
            http_req.add_header("Content-Type", "application/json")
            resp = urlopen(http_req, timeout=600)
            success = resp.status == 200
        except Exception:
            success = False

        with _queue_lock:
            req["finished_at"] = time.time()
            if success:
                req["status"] = "completed"
                req["result"] = "backup completed successfully"
            else:
                req["status"] = "failed"
                req["result"] = "backup process exited with an error"

        # Fire callbacks in a separate thread so we don't block the queue
        payload = {
            "request_id": req["request_id"],
            "status": req["status"],
            "result": req["result"],
            "params": req["params"],
            "started_at": req["started_at"],
            "finished_at": req["finished_at"],
            "duration_seconds": round(req["finished_at"] - req["started_at"], 1),
        }
        threading.Thread(target=_fire_callbacks, args=(req, payload), daemon=True).start()


def _get_request_status(request_id):
    """Look up a backup request by ID. Returns (dict, error)."""
    with _queue_lock:
        _prune_finished_requests()
        req = _requests.get(request_id)

    if not req:
        return None, f"request not found: {request_id}"

    info = {
        "request_id": req["request_id"],
        "status": req["status"],
        "params": req["params"],
        "created_at": req["created_at"],
        "started_at": req["started_at"],
        "finished_at": req["finished_at"],
        "result": req["result"],
    }

    # Add position if still queued
    if req["status"] == "queued":
        with _queue_lock:
            try:
                info["position"] = _queue.index(req) + 1
            except ValueError:
                pass

    # Add duration if finished
    if req["started_at"] and req["finished_at"]:
        info["duration_seconds"] = round(req["finished_at"] - req["started_at"], 1)

    return info, None


def _get_queue_info():
    """Build the response payload for GET /api/queue."""
    with _queue_lock:
        _prune_finished_requests()
        queued = [r for r in _queue if r["status"] == "queued"]
        running = [r for r in _requests.values() if r["status"] == "running"]

    # Estimate wait time: average of recent completed backups
    recent_durations = []
    with _queue_lock:
        for req in _requests.values():
            if req["status"] == "completed" and req["started_at"] and req["finished_at"]:
                recent_durations.append(req["finished_at"] - req["started_at"])

    avg_duration = (sum(recent_durations) / len(recent_durations)) if recent_durations else None

    items = []
    for i, req in enumerate(queued):
        entry = {
            "request_id": req["request_id"],
            "position": i + 1,
            "params": req["params"],
            "created_at": req["created_at"],
        }
        if avg_duration is not None:
            # Position in queue + 1 for currently running backup
            ahead = i + (1 if running else 0)
            entry["estimated_wait_seconds"] = round(ahead * avg_duration, 1)
        items.append(entry)

    result = {
        "queue_size": len(queued),
        "backup_running": bool(running),
        "items": items,
    }
    if avg_duration is not None:
        result["average_backup_duration_seconds"] = round(avg_duration, 1)

    return result


# ── Helpers ─────────────────────────────────────────────────────────────────

def _backup_is_running():
    """Check whether a backup is currently in progress via the lock file."""
    try:
        fd = os.open(str(LOCKFILE), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except OSError:
            return True
        finally:
            os.close(fd)
    except OSError:
        return False


def _last_success():
    """Return the epoch timestamp of the last successful backup, or None."""
    try:
        return int(LAST_SUCCESS_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _disk_info(path):
    """Return disk usage dict for the filesystem containing *path*."""
    try:
        st = os.statvfs(str(path))
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bavail
        used = total - free
        pct = round(used / total * 100, 1) if total else 0
        return {
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
            "used_percent": pct,
        }
    except OSError:
        return None


def _s3_list_keys():
    """Return a set of S3 object keys under the configured prefix.

    Calls ``aws s3api list-objects-v2`` once and streams the results.
    Returns an empty set if S3 is disabled or the call fails.
    """
    if not S3_ENABLED or not S3_BUCKET:
        return set()

    prefix = f"{S3_PREFIX}/" if S3_PREFIX else ""
    cmd = [
        "aws", "s3api", "list-objects-v2",
        "--bucket", S3_BUCKET,
        "--prefix", f"{prefix}by-id/",
        "--query", "Contents[].Key",
        "--output", "json",
        "--region", S3_REGION,
    ]
    if S3_ENDPOINT_URL:
        cmd += ["--endpoint-url", S3_ENDPOINT_URL]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return set()
        keys = json.loads(result.stdout or "null")
        if not keys:
            return set()
        # Strip the bucket prefix so keys are relative (e.g. "by-id/...")
        stripped = set()
        for k in keys:
            if prefix and k.startswith(prefix):
                stripped.add(k[len(prefix):])
            else:
                stripped.add(k)
        return stripped
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return set()


def _read_index():
    """Read and return the camera index from _index.json.

    Returns the parsed dict. If the file does not exist or is invalid,
    returns a default structure with an empty cameras list.
    """
    default = {"cameras": []}
    try:
        data = json.loads(INDEX_FILE.read_text())
        if not isinstance(data, dict) or "cameras" not in data:
            return default
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_index(data):
    """Write the camera index to _index.json.

    Creates the file (and parent directory) if it does not exist.
    """
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _build_cameras():
    """Build the response payload for GET /api/cameras."""
    data = _read_index()
    cameras = data.get("cameras", [])
    return {
        "cameras": cameras,
        "total": len(cameras),
        "enabled": sum(1 for c in cameras if c.get("enabled", True)),
    }


def _archive_ranges():
    """Scan local archive and return per-camera date ranges.

    Returns dict: {camera_id: {"oldest_ms": int, "newest_ms": int, "recording_count": int}}
    """
    by_id = ARCHIVE_DIR / "by-id"
    ranges = {}
    if not by_id.is_dir():
        return ranges

    for mp4 in by_id.rglob("*.mp4"):
        parsed = _parse_recording_filename(mp4.name)
        if not parsed:
            continue
        seg_start, seg_end = parsed
        # Camera ID is the first directory component under by-id/
        cam_id = mp4.relative_to(by_id).parts[0]

        if cam_id not in ranges:
            ranges[cam_id] = {
                "oldest_ms": seg_start,
                "newest_ms": seg_end,
                "recording_count": 0,
            }
        r = ranges[cam_id]
        r["oldest_ms"] = min(r["oldest_ms"], seg_start)
        r["newest_ms"] = max(r["newest_ms"], seg_end)
        r["recording_count"] += 1

    return ranges


def _s3_ranges():
    """Derive per-camera date ranges from S3 object keys.

    Returns dict: {camera_id: {"oldest_ms": int, "newest_ms": int, "recording_count": int}}
    """
    keys = _s3_list_keys()
    if not keys:
        return {}

    ranges = {}
    for key in keys:
        if not key.startswith("by-id/"):
            continue
        fname = key.rsplit("/", 1)[-1]
        if not fname.endswith(".mp4"):
            continue
        parsed = _parse_recording_filename(fname)
        if not parsed:
            continue
        seg_start, seg_end = parsed
        # by-id/{camera_id}/...
        cam_id = key.split("/")[1] if len(key.split("/")) > 1 else None
        if not cam_id:
            continue

        if cam_id not in ranges:
            ranges[cam_id] = {
                "oldest_ms": seg_start,
                "newest_ms": seg_end,
                "recording_count": 0,
            }
        r = ranges[cam_id]
        r["oldest_ms"] = min(r["oldest_ms"], seg_start)
        r["newest_ms"] = max(r["newest_ms"], seg_end)
        r["recording_count"] += 1

    return ranges


def _unvr_ranges(camera_id=None):
    """Query the UNVR for per-camera recording date ranges via the exporter.

    If *camera_id* is given, limits the query to that single camera.

    Returns dict: {camera_id: {"oldest_ms": int, "newest_ms": int, "recording_count": int}}
    Raises RuntimeError on failure.
    """
    url = f"{EXPORTER_URL}/unvr/ranges"
    if camera_id:
        url += f"?camera_id={camera_id}"

    try:
        resp = urlopen(Request(url), timeout=20)
        data = json.loads(resp.read())
        return data.get("ranges", {})
    except Exception as exc:
        # Try to extract the error message from a JSON error response
        if hasattr(exc, "read"):
            try:
                err = json.loads(exc.read()).get("error", str(exc))
                raise RuntimeError(err)
            except (json.JSONDecodeError, AttributeError):
                pass
        raise RuntimeError(f"exporter query failed: {exc}")


def _build_cameras_detail(camera_id=None):
    """Build enriched camera list with archive/S3/UNVR date ranges.

    If *camera_id* is given, returns detail for a single camera only.
    Returns (result, error).
    """
    data = _read_index()
    cameras = data.get("cameras", [])

    if camera_id:
        cameras = [c for c in cameras if c.get("id") == camera_id]
        if not cameras:
            return None, f"camera not found: {camera_id}"

    # Local archive ranges (always available, fast)
    local = _archive_ranges()

    # S3 ranges (skipped if S3 disabled)
    s3 = _s3_ranges() if S3_ENABLED else {}

    # UNVR ranges (SSH query — may fail)
    unvr = {}
    unvr_error = None
    try:
        unvr = _unvr_ranges(camera_id=camera_id)
    except RuntimeError as exc:
        unvr_error = str(exc)

    enriched = []
    for cam in cameras:
        cid = cam.get("id", "")
        entry = {
            "id": cid,
            "name": cam.get("name"),
            "timezone": cam.get("timezone") or _get_device_timezone(),
            "enabled": cam.get("enabled", True),
            "archive": local.get(cid),
            "s3": s3.get(cid) if S3_ENABLED else None,
            "unvr": unvr.get(cid),
        }
        enriched.append(entry)

    result = {
        "cameras": enriched,
        "total": len(enriched),
        "enabled": sum(1 for c in enriched if c.get("enabled", True)),
    }
    if unvr_error:
        result["unvr_error"] = unvr_error

    # For single-camera requests, flatten to just the camera object
    if camera_id:
        single = enriched[0] if enriched else None
        if unvr_error and single:
            single["unvr_error"] = unvr_error
        return single, None

    return result, None


def _add_camera(camera_id, name=None, enabled=True, timezone=None):
    """Add a camera to the index. Returns (result, error)."""
    if not camera_id or not isinstance(camera_id, str):
        return None, "camera_id is required and must be a string"

    data = _read_index()
    cameras = data.get("cameras", [])

    # Check for duplicate
    for cam in cameras:
        if cam.get("id") == camera_id:
            return None, f"camera already exists: {camera_id}"

    entry = {"id": camera_id, "enabled": enabled}
    if name:
        entry["name"] = name
    if timezone:
        entry["timezone"] = timezone

    cameras.append(entry)
    data["cameras"] = cameras
    _write_index(data)

    return entry, None


def _remove_camera(camera_id):
    """Remove a camera from the index. Returns (result, error)."""
    if not camera_id or not isinstance(camera_id, str):
        return None, "camera_id is required and must be a string"

    data = _read_index()
    cameras = data.get("cameras", [])

    original_len = len(cameras)
    cameras = [c for c in cameras if c.get("id") != camera_id]

    if len(cameras) == original_len:
        return None, f"camera not found: {camera_id}"

    data["cameras"] = cameras
    _write_index(data)

    return {"removed": camera_id}, None


def _update_camera(camera_id, name=None, enabled=None, timezone=None):
    """Update a camera in the index. Returns (result, error)."""
    if not camera_id or not isinstance(camera_id, str):
        return None, "camera_id is required and must be a string"

    data = _read_index()
    cameras = data.get("cameras", [])

    for cam in cameras:
        if cam.get("id") == camera_id:
            if name is not None:
                cam["name"] = name
            if enabled is not None:
                cam["enabled"] = enabled
            if timezone is not None:
                cam["timezone"] = timezone
            _write_index(data)
            return cam, None

    return None, f"camera not found: {camera_id}"


def _query_unvr_cameras():
    """Query the UNVR cameras table via the exporter.

    Returns a list of dicts [{"id": "...", "name": "..."}, ...]
    or raises RuntimeError on failure.
    """
    url = f"{EXPORTER_URL}/unvr/cameras"

    try:
        resp = urlopen(Request(url), timeout=20)
        data = json.loads(resp.read())
        return data.get("cameras", [])
    except Exception as exc:
        if hasattr(exc, "read"):
            try:
                err = json.loads(exc.read()).get("error", str(exc))
                raise RuntimeError(err)
            except (json.JSONDecodeError, AttributeError):
                pass
        raise RuntimeError(f"exporter query failed: {exc}")


def _compute_sync_changes(unvr_cameras):
    """Compare UNVR cameras against the current index and compute a change set.

    Returns (changes, index_data) where changes is a list of dicts describing
    each action:
      {"action": "add"|"disable"|"update_name", "camera": {...}, ...}
    """
    data = _read_index()
    index_cams = {c["id"]: c for c in data.get("cameras", [])}
    unvr_map = {c["id"]: c for c in unvr_cameras}

    changes = []

    # Cameras on UNVR but not in index → add as disabled
    for uid, ucam in unvr_map.items():
        if uid not in index_cams:
            changes.append({
                "action": "add",
                "camera_id": uid,
                "name": ucam["name"],
                "enabled": False,
                "reason": "exists on UNVR but not in config",
            })

    # Cameras in index but not on UNVR → disable
    for iid, icam in index_cams.items():
        if iid not in unvr_map:
            if icam.get("enabled", True):
                changes.append({
                    "action": "disable",
                    "camera_id": iid,
                    "name": icam.get("name"),
                    "reason": "exists in config but not on UNVR",
                })

    # Cameras in both → check name
    for iid, icam in index_cams.items():
        if iid in unvr_map:
            ucam = unvr_map[iid]
            unvr_name = ucam["name"]
            index_name = icam.get("name")
            if index_name != unvr_name:
                changes.append({
                    "action": "update_name",
                    "camera_id": iid,
                    "old_name": index_name,
                    "new_name": unvr_name,
                    "reason": "name differs between UNVR and config",
                })

    return changes, data


def _apply_sync_changes(changes, data):
    """Apply computed sync changes to the index data and write it.

    Returns the updated cameras list.
    """
    index_cams = {c["id"]: c for c in data.get("cameras", [])}

    for change in changes:
        action = change["action"]
        cid = change["camera_id"]

        if action == "add":
            entry = {"id": cid, "name": change["name"], "enabled": False}
            index_cams[cid] = entry

        elif action == "disable":
            if cid in index_cams:
                index_cams[cid]["enabled"] = False

        elif action == "update_name":
            if cid in index_cams:
                index_cams[cid]["name"] = change["new_name"]

    # Rebuild the cameras list sorted by name for consistency
    cameras = sorted(index_cams.values(), key=lambda c: c.get("name", ""))
    data["cameras"] = cameras
    data["last_synced"] = int(time.time())
    _write_index(data)

    return cameras


def _build_status():
    """Build the response payload for GET /api/status."""
    now = int(time.time())
    last = _last_success()

    status = {
        "status": "running" if _backup_is_running() else "idle",
        "last_success_epoch": last,
        "last_success_age_seconds": (now - last) if last else None,
        "cron_schedule": CRON_SCHEDULE,
        "archive": _disk_info(ARCHIVE_DIR),
        "s3": {
            "enabled": S3_ENABLED,
            "bucket": S3_BUCKET or None,
            "prefix": S3_PREFIX or None,
            "region": S3_REGION,
            "storage_class": S3_STORAGE_CLASS,
            "delete_local": S3_DELETE_LOCAL,
        },
        "camera_index": _build_cameras(),
        "last_synced": _read_index().get("last_synced"),
        "queue": _get_queue_info(),
    }
    return status


def _build_backups():
    """Build the response payload for GET /api/backups."""
    by_id = ARCHIVE_DIR / "by-id"
    cameras = {}
    total_local = 0

    # Walk local archive
    if by_id.is_dir():
        for mp4 in sorted(by_id.rglob("*.mp4")):
            rel = mp4.relative_to(by_id)
            parts = rel.parts  # e.g. ("63a1f2bc...", "2026-02-19", "file.mp4")
            cam = parts[0]

            if cam not in cameras:
                cameras[cam] = []

            try:
                size = mp4.stat().st_size
            except OSError:
                size = 0

            cameras[cam].append({
                "file": mp4.name,
                "path": str(rel),
                "size_bytes": size,
                "local": True,
                "s3": False,  # updated below
            })
            total_local += 1

    # Cross-reference with S3
    total_s3 = 0
    s3_keys = _s3_list_keys()

    if s3_keys:
        # Mark local files that also exist in S3
        for cam, recordings in cameras.items():
            for rec in recordings:
                s3_key = f"by-id/{rec['path']}"
                if s3_key in s3_keys:
                    rec["s3"] = True
                    total_s3 += 1
                    s3_keys.discard(s3_key)

        # Add S3-only files (local was deleted via S3_DELETE_LOCAL)
        for key in sorted(s3_keys):
            if not key.startswith("by-id/"):
                continue
            rel = key[len("by-id/"):]
            parts = Path(rel).parts
            if len(parts) < 2:
                continue
            cam = parts[0]
            fname = parts[-1]
            if not fname.endswith(".mp4"):
                continue

            if cam not in cameras:
                cameras[cam] = []

            cameras[cam].append({
                "file": fname,
                "path": rel,
                "size_bytes": None,
                "local": False,
                "s3": True,
            })
            total_s3 += 1

    # Sort recordings within each camera by filename (chronological)
    for cam in cameras:
        cameras[cam].sort(key=lambda r: r["file"])

    return {
        "cameras": {cam: cameras[cam] for cam in sorted(cameras)},
        "total_recordings": total_local + sum(
            1 for recs in cameras.values() for r in recs if r["s3"] and not r["local"]
        ),
        "total_local": total_local,
        "total_s3": total_s3,
    }


# Regex for parsing recording filenames produced by backup.sh:
#   {cam_id}_{YYYY-MM-DD}_{HH-MM-SS}_to_{HH-MM-SS}.mp4
#   {cam_id}_{YYYY-MM-DD}_{HH-MM-SS}_to_{HH-MM-SS}_{label}.mp4
_RECORDING_RE = re.compile(
    r"^(?P<cam>.+)_(?P<date>\d{4}-\d{2}-\d{2})"
    r"_(?P<start>\d{2}-\d{2}-\d{2})_to_(?P<end>\d{2}-\d{2}-\d{2})"
    r"(?:_(?P<label>[a-z0-9-]+))?\.mp4$"
)


def _hms_to_seconds(hms):
    """Convert 'HH-MM-SS' to seconds since midnight."""
    h, m, s = hms.split("-")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _date_hms_to_epoch_ms(date_str, hms):
    """Convert 'YYYY-MM-DD' + 'HH-MM-SS' to epoch milliseconds (UTC)."""
    y, mo, d = date_str.split("-")
    h, mi, s = hms.split("-")
    ts = calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), int(s)))
    return ts * 1000


def _parse_recording_filename(filename):
    """Extract start/end epoch-ms from a recording filename.

    Returns (start_ms, end_ms) or None if the filename doesn't match.
    """
    m = _RECORDING_RE.match(filename)
    if not m:
        return None
    date_str = m.group("date")
    start_hms = m.group("start")
    end_hms = m.group("end")

    start_ms = _date_hms_to_epoch_ms(date_str, start_hms)

    # Handle recordings that span midnight: end time < start time means next day
    end_ms = _date_hms_to_epoch_ms(date_str, end_hms)
    if _hms_to_seconds(end_hms) <= _hms_to_seconds(start_hms):
        end_ms += 86400 * 1000  # add one day

    return start_ms, end_ms


def _build_playback(camera_id, range_start_ms, range_end_ms):
    """Build the response payload for GET /api/playback.

    Finds all recordings for *camera_id* that overlap [range_start, range_end],
    sorts them chronologically, and computes seek offsets for the first and last
    file so a player can cover exactly the requested range.
    """
    cam_dir = ARCHIVE_DIR / "by-id" / camera_id
    if not cam_dir.is_dir():
        return None, f"camera not found: {camera_id}"

    # Collect all .mp4 files with parsed timestamps
    segments = []
    for mp4 in cam_dir.rglob("*.mp4"):
        parsed = _parse_recording_filename(mp4.name)
        if not parsed:
            continue
        seg_start, seg_end = parsed

        # Keep segments that overlap the requested range
        if seg_end > range_start_ms and seg_start < range_end_ms:
            segments.append({
                "file": mp4.name,
                "path": str(mp4.relative_to(ARCHIVE_DIR / "by-id")),
                "recording_start_ms": seg_start,
                "recording_end_ms": seg_end,
            })

    if not segments:
        return None, "no recordings found for the requested range"

    # Sort by recording start time
    segments.sort(key=lambda s: s["recording_start_ms"])

    # Compute seek offsets
    first = segments[0]
    last = segments[-1]

    # How far into the first file the requested range begins
    start_offset_ms = max(0, range_start_ms - first["recording_start_ms"])
    # How far into the last file the requested range ends
    end_offset_ms = min(
        last["recording_end_ms"] - last["recording_start_ms"],
        range_end_ms - last["recording_start_ms"],
    )

    return {
        "camera_id": camera_id,
        "range": {
            "start_ms": range_start_ms,
            "end_ms": range_end_ms,
        },
        "files": segments,
        "playback": {
            "start_offset_ms": start_offset_ms,
            "end_offset_ms": end_offset_ms,
            "start_offset_seconds": round(start_offset_ms / 1000, 1),
            "end_offset_seconds": round(end_offset_ms / 1000, 1),
            "total_files": len(segments),
        },
    }, None


def _trigger_backup(camera_id=None, start=None, end=None, callback_url=None):
    """Trigger a backup, either immediately or via the queue.

    If no backup is running and the queue is empty, launches backup.sh
    directly and returns immediately. Otherwise the request is queued
    and will execute once all preceding requests have finished.

    Every request receives a unique ``request_id`` for status tracking.
    An optional *callback_url* will be POSTed with the result payload
    when the request finishes processing.

    Returns a dict suitable for the JSON response.
    """
    # Build the canonical request ID regardless of path
    request_id = str(uuid.uuid4())
    params = {}
    if camera_id:
        params["camera_id"] = camera_id
    if start is not None:
        params["start"] = int(start)
    if end is not None:
        params["end"] = int(end)

    running = _backup_is_running()
    queue_non_empty = bool(_queue)

    # If system is busy (backup running or queue non-empty), enqueue
    if running or queue_non_empty:
        result, err = _enqueue_backup(
            camera_id=camera_id, start=start, end=end,
            callback_url=callback_url,
        )
        if err:
            return None, err
        return result, None

    # System is idle — run immediately but still track the request
    key = _dedup_key(camera_id, start, end)
    req = {
        "request_id": request_id,
        "status": "running",
        "params": params if params else "defaults",
        "callback_urls": [callback_url] if callback_url else [],
        "created_at": time.time(),
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
        "_dedup_key": key,
        "_camera_id": camera_id,
        "_start": start,
        "_end": end,
    }
    with _queue_lock:
        _requests[request_id] = req

    # Build the trigger request for the exporter
    trigger_body = {}
    if camera_id:
        trigger_body["camera_id"] = str(camera_id)
    if start is not None:
        trigger_body["start"] = int(start)
    if end is not None:
        trigger_body["end"] = int(end)

    def _run_and_track():
        try:
            data = json.dumps(trigger_body).encode()
            http_req = Request(
                f"{EXPORTER_URL}/trigger",
                data=data, method="POST",
            )
            http_req.add_header("Content-Type", "application/json")
            resp = urlopen(http_req, timeout=600)
            success = resp.status == 200
        except Exception:
            success = False

        with _queue_lock:
            req["finished_at"] = time.time()
            if success:
                req["status"] = "completed"
                req["result"] = "backup completed successfully"
            else:
                req["status"] = "failed"
                req["result"] = "backup process exited with an error"

        payload = {
            "request_id": req["request_id"],
            "status": req["status"],
            "result": req["result"],
            "params": req["params"],
            "started_at": req["started_at"],
            "finished_at": req["finished_at"],
            "duration_seconds": round(req["finished_at"] - req["started_at"], 1),
        }
        threading.Thread(target=_fire_callbacks, args=(req, payload), daemon=True).start()

    try:
        threading.Thread(target=_run_and_track, daemon=True).start()
    except Exception as exc:
        return None, f"failed to start backup: {exc}"

    return {
        "request_id": request_id,
        "triggered": True,
        "queued": False,
        "params": params if params else "defaults",
    }, None


# ── HTTP handler ────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    """Minimal JSON API handler."""

    def handle(self):
        """Wrap request handling to absorb broken-pipe / reset errors."""
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        """Read and parse a JSON request body, or return {} if empty."""
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            return None

    def _parse_query(self):
        """Parse query string parameters from the request URL."""
        parsed = urlparse(self.path)
        return parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}

    def do_GET(self):
        path, qs = self._parse_query()

        if path == "/api/status":
            self._send_json(_build_status())
        elif path == "/api/backups":
            self._send_json(_build_backups())
        elif path == "/api/playback":
            # Validate required parameters
            camera_id = qs.get("camera_id")
            start_str = qs.get("start")
            end_str = qs.get("end")

            if not camera_id or not start_str or not end_str:
                self._send_json({
                    "error": "camera_id, start, and end query parameters are required",
                    "usage": "/api/playback?camera_id=<id>&start=<epoch_ms>&end=<epoch_ms>",
                }, status=400)
                return

            try:
                start_ms = int(start_str)
                end_ms = int(end_str)
            except ValueError:
                self._send_json(
                    {"error": "start and end must be integers (epoch ms)"},
                    status=400,
                )
                return

            if start_ms >= end_ms:
                self._send_json(
                    {"error": "start must be before end"},
                    status=400,
                )
                return

            result, err = _build_playback(camera_id, start_ms, end_ms)
            if err:
                self._send_json({"error": err}, status=404)
            else:
                self._send_json(result)

        elif path == "/api/cameras/sync":
            try:
                unvr_cameras = _query_unvr_cameras()
            except RuntimeError as exc:
                self._send_json(
                    {"error": str(exc)}, status=502,
                )
                return

            changes, _ = _compute_sync_changes(unvr_cameras)
            self._send_json({
                "unvr_cameras": len(unvr_cameras),
                "changes": changes,
                "total_changes": len(changes),
            })

        elif path == "/api/cameras":
            # List all cameras
            result, err = _build_cameras_detail()
            if err:
                self._send_json({"error": err}, status=404)
            else:
                self._send_json(result)

        elif path.startswith("/api/cameras/") and path.count("/") == 3:
            # Single camera by ID: /api/cameras/{camera_id}
            camera_id = path.split("/")[3]
            if not camera_id:
                self._send_json(
                    {"error": "camera_id is required in the URL path"},
                    status=400,
                )
                return
            result, err = _build_cameras_detail(camera_id=camera_id)
            if err:
                self._send_json({"error": err}, status=404)
            else:
                self._send_json(result)

        elif path == "/api/queue":
            self._send_json(_get_queue_info())

        elif path.startswith("/api/backup/") and path.count("/") == 3:
            request_id = path.split("/")[3]
            if not request_id:
                self._send_json(
                    {"error": "request_id is required in the URL path"},
                    status=400,
                )
                return
            result, err = _get_request_status(request_id)
            if err:
                self._send_json({"error": err}, status=404)
            else:
                self._send_json(result)

        elif path == "/api/health":
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found", "endpoints": [
                "GET    /api/status",
                "GET    /api/backups",
                "GET    /api/playback?camera_id=<id>&start=<epoch_ms>&end=<epoch_ms>",
                "POST   /api/backup",
                "GET    /api/backup/{request_id}",
                "GET    /api/queue",
                "GET    /api/cameras",
                "GET    /api/cameras/{id}",
                "POST   /api/cameras",
                "PATCH  /api/cameras/{id}",
                "DELETE /api/cameras/{id}",
                "GET    /api/cameras/sync",
                "POST   /api/cameras/sync",
                "GET    /api/health",
            ]}, status=404)

    def do_POST(self):
        path, _ = self._parse_query()

        if path == "/api/backup":
            body = self._read_json_body()
            if body is None:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return

            # Validate parameters
            camera_id = body.get("camera_id")
            start = body.get("start")
            end = body.get("end")
            callback_url = body.get("callback_url")

            if camera_id is not None and not isinstance(camera_id, str):
                self._send_json({"error": "camera_id must be a string"}, status=400)
                return
            for field, value in [("start", start), ("end", end)]:
                if value is not None:
                    if not isinstance(value, (int, float)) or value < 0:
                        self._send_json(
                            {"error": f"{field} must be a positive number (epoch ms)"},
                            status=400,
                        )
                        return
            if callback_url is not None and not isinstance(callback_url, str):
                self._send_json({"error": "callback_url must be a string"}, status=400)
                return

            result, err = _trigger_backup(
                camera_id=camera_id, start=start, end=end,
                callback_url=callback_url,
            )
            if err:
                self._send_json({"error": err}, status=409)
            else:
                self._send_json(result, status=202)

        elif path == "/api/cameras/sync":
            try:
                unvr_cameras = _query_unvr_cameras()
            except RuntimeError as exc:
                self._send_json(
                    {"error": str(exc)}, status=502,
                )
                return

            changes, data = _compute_sync_changes(unvr_cameras)
            if not changes:
                # Still update the sync timestamp
                data["last_synced"] = int(time.time())
                _write_index(data)
                self._send_json({
                    "synced": True,
                    "changes_applied": 0,
                    "cameras": data.get("cameras", []),
                    "last_synced": data["last_synced"],
                })
                return

            cameras = _apply_sync_changes(changes, data)
            self._send_json({
                "synced": True,
                "changes_applied": len(changes),
                "changes": changes,
                "cameras": cameras,
                "last_synced": data["last_synced"],
            })

        elif path == "/api/cameras":
            body = self._read_json_body()
            if body is None:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return

            camera_id = body.get("camera_id") or body.get("id")
            name = body.get("name")
            enabled = body.get("enabled", True)
            timezone = body.get("timezone")

            if not camera_id:
                self._send_json(
                    {"error": "camera_id is required"},
                    status=400,
                )
                return

            if not isinstance(enabled, bool):
                self._send_json(
                    {"error": "enabled must be a boolean"},
                    status=400,
                )
                return

            result, err = _add_camera(camera_id, name=name, enabled=enabled, timezone=timezone)
            if err:
                self._send_json({"error": err}, status=409)
            else:
                self._send_json(result, status=201)
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_DELETE(self):
        path, _ = self._parse_query()

        # Match DELETE /api/cameras/{camera_id}
        if path.startswith("/api/cameras/") and path.count("/") == 3:
            camera_id = path.split("/")[3]
            if not camera_id:
                self._send_json(
                    {"error": "camera_id is required in the URL path"},
                    status=400,
                )
                return

            result, err = _remove_camera(camera_id)
            if err:
                self._send_json({"error": err}, status=404)
            else:
                self._send_json(result)
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_PATCH(self):
        path, _ = self._parse_query()

        # Match /api/cameras/{camera_id}
        if path.startswith("/api/cameras/") and path.count("/") == 3:
            camera_id = path.split("/")[3]
            if not camera_id:
                self._send_json(
                    {"error": "camera_id is required in the URL path"},
                    status=400,
                )
                return

            body = self._read_json_body()
            if body is None:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return

            name = body.get("name")
            enabled = body.get("enabled")
            timezone = body.get("timezone")

            if enabled is not None and not isinstance(enabled, bool):
                self._send_json(
                    {"error": "enabled must be a boolean"},
                    status=400,
                )
                return

            if name is None and enabled is None and timezone is None:
                self._send_json(
                    {"error": "at least one of name, enabled, or timezone is required"},
                    status=400,
                )
                return

            result, err = _update_camera(camera_id, name=name, enabled=enabled, timezone=timezone)
            if err:
                self._send_json({"error": err}, status=404)
            else:
                self._send_json(result)
        else:
            self._send_json({"error": "not found"}, status=404)

    def log_message(self, format, *args):
        """Suppress default stderr logging — container logs are noisy enough."""
        pass


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Start the background queue processor thread
    _queue_thread = threading.Thread(target=_process_queue, daemon=True)
    _queue_thread.start()
    print("[api] Queue processor started", flush=True)

    server = HTTPServer(("0.0.0.0", API_PORT), Handler)
    print(f"[api] Listening on port {API_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
