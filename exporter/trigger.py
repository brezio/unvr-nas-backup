#!/usr/bin/env python3
"""Minimal internal trigger server for the exporter container.

Listens on EXPORTER_PORT (default 8550) and accepts POST /trigger requests
from the API service. Runs backup.sh synchronously and returns the result.
This is an internal endpoint — not exposed outside the Docker network.
"""

import json
import os
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler

EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "8550"))


class TriggerHandler(BaseHTTPRequestHandler):
    """Handle POST /trigger to run backup.sh synchronously."""

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        if self.path == "/health":
            self._send_json({"ok": True})
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
