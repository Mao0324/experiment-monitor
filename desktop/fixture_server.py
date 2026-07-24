"""One-request localhost fixture used to smoke-test the compiled Windows client."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/api/v1/public/runs":
            self.send_error(404)
            return
        payload = json.dumps(
            {
                "runs": [
                    {
                        "id": "desktop-smoke",
                        "name": "Desktop smoke test",
                        "status": "running",
                        "current_epoch": 1,
                        "total_epochs": 100,
                    }
                ],
                "server_time": "2026-07-19T00:00:00+00:00",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1])
    server = HTTPServer(("127.0.0.1", port), Handler)
    if len(sys.argv) == 2:
        server.handle_request()
    else:
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        completed = subprocess.run(
            [sys.argv[2], "--connection-test", f"http://127.0.0.1:{port}"],
            timeout=15,
            check=False,
        )
        thread.join(timeout=5)
        server.server_close()
        raise SystemExit(completed.returncode if not thread.is_alive() else 3)
