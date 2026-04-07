#!/usr/bin/env python3
"""Lightweight status API for unvr-nas-backup.

Runs inside the container alongside cron. Uses only the Python standard library
so no pip dependencies are required. Reads the same environment variables and
file-system state that backup.sh uses.

Endpoints:
    GET  /api/status   — current backup health and configuration
    GET  /api/backups  — list all archived recordings with local/S3 location
    POST /api/backup   — trigger a backup (optional camera_id and time range)
"""

import json
import os
import subprocess
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

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
        "--prefix", f"{prefix}by-camera/",
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
        # Strip the bucket prefix so keys are relative (e.g. "by-camera/...")
        stripped = set()
        for k in keys:
            if prefix and k.startswith(prefix):
                stripped.add(k[len(prefix):])
            else:
                stripped.add(k)
        return stripped
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return set()


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
    }
    return status


def _build_backups():
    """Build the response payload for GET /api/backups."""
    by_camera = ARCHIVE_DIR / "by-camera"
    cameras = {}
    total_local = 0

    # Walk local archive
    if by_camera.is_dir():
        for mp4 in sorted(by_camera.rglob("*.mp4")):
            rel = mp4.relative_to(by_camera)
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
                s3_key = f"by-camera/{rec['path']}"
                if s3_key in s3_keys:
                    rec["s3"] = True
                    total_s3 += 1
                    s3_keys.discard(s3_key)

        # Add S3-only files (local was deleted via S3_DELETE_LOCAL)
        for key in sorted(s3_keys):
            if not key.startswith("by-camera/"):
                continue
            rel = key[len("by-camera/"):]
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

    def do_GET(self):
        if self.path == "/api/status":
            self._send_json(_build_status())
        elif self.path == "/api/backups":
            self._send_json(_build_backups())
        elif self.path == "/api/health":
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found", "endpoints": [
                "GET  /api/status",
                "GET  /api/backups",
                "POST /api/backup",
                "GET  /api/health",
            ]}, status=404)

    def do_POST(self):
        if self.path == "/api/backup":
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
