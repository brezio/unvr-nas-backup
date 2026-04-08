#!/usr/bin/env python3
"""Internal service endpoints for the exporter container.

Listens on EXPORTER_PORT (default 8550) and provides:
  POST /trigger                     — run backup.sh synchronously
  GET  /unvr/ranges[?camera_id=ID]  — query UNVR for per-camera recording ranges
  GET  /unvr/cameras                — query UNVR for the cameras table
  GET  /health                      — liveness check

These are internal endpoints called by the API service over the Docker
network — they are not exposed to the host.
"""

import json
import os
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "8550"))

PROTECT_HOST = os.environ.get("PROTECT_HOST", "")
PROTECT_SSH_USER = os.environ.get("PROTECT_SSH_USER", "root")
PROTECT_DB_PORT = os.environ.get("PROTECT_DB_PORT", "5433")
PROTECT_DB_NAME = os.environ.get("PROTECT_DB_NAME", "unifi-protect")
SSH_OPTS = os.environ.get("SSH_OPTS", "")


def _ssh_query(sql):
    """Run a SQL query on the UNVR via SSH and return stdout.

    Raises RuntimeError on failure.
    """
    if not PROTECT_HOST or not SSH_OPTS:
        raise RuntimeError("PROTECT_HOST or SSH_OPTS not configured")

    ssh_cmd = SSH_OPTS.split() + [
        f"{PROTECT_SSH_USER}@{PROTECT_HOST}",
        f"psql -p {PROTECT_DB_PORT} -U postgres -d {PROTECT_DB_NAME} -At -F,",
    ]

    try:
        result = subprocess.run(
            ["ssh"] + ssh_cmd,
            input=sql, capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("SSH connection to UNVR timed out")

    if result.returncode != 0:
        stderr = result.stderr.strip()[:200]
        raise RuntimeError(f"UNVR query failed: {stderr}")

    return result.stdout


def _unvr_ranges(camera_id=None):
    """Query UNVR for per-camera recording date ranges."""
    where_clause = ""
    if camera_id:
        where_clause = f"WHERE c.id = '{camera_id}'"

    sql = (
        f'SELECT c.id, MIN(rf.start), MAX(rf."end"), COUNT(*) '
        f'FROM cameras c '
        f'JOIN "recordingFiles" rf ON c.id = rf."cameraId" '
        f'{where_clause} '
        f'GROUP BY c.id'
    )

    stdout = _ssh_query(sql)
    ranges = {}
    for line in stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 4:
            continue
        cid = parts[0].strip()
        try:
            oldest = int(parts[1].strip())
            newest = int(parts[2].strip())
            count = int(parts[3].strip())
        except ValueError:
            continue
        ranges[cid] = {
            "oldest_ms": oldest,
            "newest_ms": newest,
            "recording_count": count,
        }
    return ranges


def _unvr_cameras():
    """Query UNVR for the cameras table (id, name)."""
    sql = "COPY (SELECT id, name FROM cameras ORDER BY name) TO STDOUT WITH CSV HEADER;"
    # Use -At without -F for COPY output
    if not PROTECT_HOST or not SSH_OPTS:
        raise RuntimeError("PROTECT_HOST or SSH_OPTS not configured")

    ssh_cmd = SSH_OPTS.split() + [
        f"{PROTECT_SSH_USER}@{PROTECT_HOST}",
        f"psql -p {PROTECT_DB_PORT} -U postgres -d {PROTECT_DB_NAME} -At",
    ]

    try:
        result = subprocess.run(
            ["ssh"] + ssh_cmd,
            input=sql, capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("SSH connection to UNVR timed out")

    if result.returncode != 0:
        stderr = result.stderr.strip()[:200]
        raise RuntimeError(f"UNVR query failed: {stderr}")

    cameras = []
    for line in result.stdout.strip().splitlines():
        if not line or line.startswith("id,"):
            continue
        parts = line.split(",", 1)
        if len(parts) >= 2:
            cameras.append({"id": parts[0].strip(), "name": parts[1].strip()})

    return cameras


TIMEZONE_CACHE_FILE = "/shared/timezone"


def _read_cached_timezone():
    """Read the device timezone cached by entrypoint.sh."""
    try:
        with open(TIMEZONE_CACHE_FILE) as f:
            tz = f.read().strip()
            return tz if tz else "UTC"
    except FileNotFoundError:
        return "UTC"


class TriggerHandler(BaseHTTPRequestHandler):
    """Handle internal requests from the API service."""

    def handle(self):
        """Wrap request handling to absorb broken-pipe / reset errors."""
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parse_query(self):
        parsed = urlparse(self.path)
        return parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}

    def do_POST(self):
        if self.path != "/trigger":
            self._send_json({"error": "not found"}, status=404)
            return

        # Read optional JSON body with override parameters
        length = int(self.headers.get("Content-Length", 0))
        body = {}
        if length > 0:
            try:
                body = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, ValueError):
                self._send_json({"error": "invalid JSON"}, status=400)
                return

        env = os.environ.copy()
        if body.get("camera_id"):
            env["BACKUP_CAMERA_ID"] = str(body["camera_id"])
        if body.get("start") is not None:
            env["BACKUP_START"] = str(int(body["start"]))
        if body.get("end") is not None:
            env["BACKUP_END"] = str(int(body["end"]))

        try:
            result = subprocess.run(
                ["/usr/local/bin/backup.sh"],
                env=env,
                stdout=open("/proc/1/fd/1", "w"),
                stderr=open("/proc/1/fd/2", "w"),
            )
            if result.returncode == 0:
                self._send_json({"ok": True, "result": "backup completed successfully"})
            else:
                self._send_json(
                    {"ok": False, "result": f"backup exited with code {result.returncode}"},
                    status=500,
                )
        except OSError as exc:
            self._send_json(
                {"ok": False, "result": f"failed to run backup: {exc}"},
                status=500,
            )

    def do_GET(self):
        path, qs = self._parse_query()

        if path == "/health":
            self._send_json({"ok": True})

        elif path == "/unvr/ranges":
            camera_id = qs.get("camera_id")
            try:
                ranges = _unvr_ranges(camera_id=camera_id)
                self._send_json({"ranges": ranges})
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, status=502)

        elif path == "/unvr/cameras":
            try:
                cameras = _unvr_cameras()
                self._send_json({"cameras": cameras})
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, status=502)

        elif path == "/unvr/timezone":
            tz = _read_cached_timezone()
            self._send_json({"timezone": tz})

        else:
            self._send_json({"error": "not found"}, status=404)

    def log_message(self, format, *args):
        """Suppress default stderr logging."""
        pass


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", EXPORTER_PORT), TriggerHandler)
    print(f"[trigger] Listening on port {EXPORTER_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
