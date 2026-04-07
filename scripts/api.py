#!/usr/bin/env python3
"""Lightweight status API for unvr-nas-backup.

Runs inside the container alongside cron. Uses only the Python standard library
so no pip dependencies are required. Reads the same environment variables and
file-system state that backup.sh uses.

Endpoints:
    GET    /api/status   — current backup health and configuration
    GET    /api/backups  — list all archived recordings with local/S3 location
    GET    /api/playback — files needed to play back a camera's footage for a time range
    POST   /api/backup   — trigger a backup (optional camera_id and time range)
    GET    /api/cameras  — list cameras in the index
    POST   /api/cameras  — add a camera to the index
    PUT    /api/cameras  — update a camera in the index
    DELETE /api/cameras  — remove a camera from the index
"""

import calendar
import json
import os
import re
import subprocess
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ── Configuration ───────────────────────────────────────────────────────────

ARCHIVE_DIR = Path("/archive")
LOCKFILE = Path("/tmp/backup.lock")
LAST_SUCCESS_FILE = Path("/tmp/backup-last-success")

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


def _add_camera(camera_id, name=None, enabled=True):
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


def _update_camera(camera_id, name=None, enabled=None):
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
            _write_index(data)
            return cam, None

    return None, f"camera not found: {camera_id}"


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


def _trigger_backup(camera_id=None, start=None, end=None):
    """Launch backup.sh in the background with optional overrides.

    Returns a dict suitable for the JSON response. The backup runs
    asynchronously — this function returns immediately after spawning.
    """
    if _backup_is_running():
        return None, "a backup is already running"

    # Build environment: inherit current env, add overrides
    env = os.environ.copy()
    if camera_id:
        env["BACKUP_CAMERA_ID"] = str(camera_id)
    if start is not None:
        env["BACKUP_START"] = str(int(start))
    if end is not None:
        env["BACKUP_END"] = str(int(end))

    try:
        subprocess.Popen(
            ["/usr/local/bin/backup.sh"],
            env=env,
            stdout=open("/proc/1/fd/1", "w"),
            stderr=open("/proc/1/fd/2", "w"),
        )
    except OSError as exc:
        return None, f"failed to start backup: {exc}"

    params = {}
    if camera_id:
        params["camera_id"] = camera_id
    if start is not None:
        params["start"] = int(start)
    if end is not None:
        params["end"] = int(end)

    return {
        "triggered": True,
        "params": params if params else "defaults",
    }, None


# ── HTTP handler ────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    """Minimal JSON API handler."""

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

        elif path == "/api/cameras":
            self._send_json(_build_cameras())
        elif path == "/api/health":
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found", "endpoints": [
                "GET    /api/status",
                "GET    /api/backups",
                "GET    /api/playback?camera_id=<id>&start=<epoch_ms>&end=<epoch_ms>",
                "POST   /api/backup",
                "GET    /api/cameras",
                "POST   /api/cameras",
                "PUT    /api/cameras",
                "DELETE /api/cameras",
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

            result, err = _trigger_backup(camera_id=camera_id, start=start, end=end)
            if err:
                self._send_json({"error": err}, status=409)
            else:
                self._send_json(result, status=202)

        elif path == "/api/cameras":
            body = self._read_json_body()
            if body is None:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return

            camera_id = body.get("camera_id") or body.get("id")
            name = body.get("name")
            enabled = body.get("enabled", True)

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

            result, err = _add_camera(camera_id, name=name, enabled=enabled)
            if err:
                self._send_json({"error": err}, status=409)
            else:
                self._send_json(result, status=201)
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_DELETE(self):
        path, qs = self._parse_query()

        if path == "/api/cameras":
            # Accept camera_id from query string or JSON body
            camera_id = qs.get("camera_id")
            if not camera_id:
                body = self._read_json_body()
                if body is None:
                    self._send_json({"error": "invalid JSON body"}, status=400)
                    return
                camera_id = body.get("camera_id") or body.get("id")

            if not camera_id:
                self._send_json(
                    {"error": "camera_id is required (query param or JSON body)"},
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

    def do_PUT(self):
        path, _ = self._parse_query()

        if path == "/api/cameras":
            body = self._read_json_body()
            if body is None:
                self._send_json({"error": "invalid JSON body"}, status=400)
                return

            camera_id = body.get("camera_id") or body.get("id")
            if not camera_id:
                self._send_json(
                    {"error": "camera_id is required"},
                    status=400,
                )
                return

            name = body.get("name")
            enabled = body.get("enabled")

            if enabled is not None and not isinstance(enabled, bool):
                self._send_json(
                    {"error": "enabled must be a boolean"},
                    status=400,
                )
                return

            if name is None and enabled is None:
                self._send_json(
                    {"error": "at least one of name or enabled is required"},
                    status=400,
                )
                return

            result, err = _update_camera(camera_id, name=name, enabled=enabled)
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
    server = HTTPServer(("0.0.0.0", API_PORT), Handler)
    print(f"[api] Listening on port {API_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
