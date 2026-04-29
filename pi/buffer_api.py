"""
buffer_api.py — Lightweight HTTP API for Air Twin Pi buffer.

Exposes buffered readings to the Windows backend for sync.
Uses only Python stdlib — no Flask or other dependencies.

Endpoints:
    GET  /buffer/status          — buffer stats
    GET  /buffer/readings        — unsynced readings (JSON)
    POST /buffer/ack             — mark readings as synced
    POST /buffer/purge           — purge old synced readings

Location: /home/pi/air-twin/pi/buffer_api.py

Run directly:
    python3 buffer_api.py

Or via systemd (see buffer_sync.service).
"""

import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Add parent to path so buffer.py can be imported
sys.path.insert(0, str(Path(__file__).parent))
import buffer as buf

log = logging.getLogger(__name__)

HOST = "0.0.0.0"
PORT = 5001   # Pi buffer API port — distinct from any other service


class BufferHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # Suppress default access log — use our logger instead
        log.debug(f"{self.address_string()} — {format % args}")

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, msg: str, status: int = 400):
        self._send_json({"error": msg}, status)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/buffer/status":
            self._send_json(buf.buffer_stats())

        elif path == "/buffer/readings":
            params = parse_qs(parsed.query)
            limit = int(params.get("limit", ["5000"])[0])
            readings = buf.get_unsynced(limit=limit)
            self._send_json({
                "count": len(readings),
                "readings": readings,
            })

        elif path == "/health":
            self._send_json({"status": "ok", "service": "buffer_api"})

        else:
            self._send_error("Not found", 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/buffer/ack":
            try:
                body = self._read_body()
                ids = body.get("ids", [])
                if not ids:
                    self._send_error("ids required")
                    return
                buf.mark_synced([int(i) for i in ids])
                self._send_json({"acknowledged": len(ids)})
            except Exception as e:
                log.error(f"Ack error: {e}")
                self._send_error(str(e), 500)

        elif path == "/buffer/purge":
            try:
                body = self._read_body()
                days = int(body.get("days", 7))
                count = buf.purge_old_synced(days=days)
                self._send_json({"purged": count})
            except Exception as e:
                self._send_error(str(e), 500)

        else:
            self._send_error("Not found", 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    buf.init_buffer()
    log.info(f"Buffer API starting on {HOST}:{PORT}")

    stats = buf.buffer_stats()
    log.info(f"Buffer status: {stats['unsynced']} unsynced readings")

    server = HTTPServer((HOST, PORT), BufferHandler)
    log.info(f"Buffer API ready — http://{HOST}:{PORT}/buffer/status")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Buffer API stopped")
        server.server_close()


if __name__ == "__main__":
    main()