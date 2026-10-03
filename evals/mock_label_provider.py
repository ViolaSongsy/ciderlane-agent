"""Local mock HTTP API for demos and agent evals; creates no real shipping labels."""

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import quote


class MockLabelHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/labels":
            return self.send_json(404, {"error": "not_found"})
        if self.headers.get("Authorization") != "Bearer mock-token":
            return self.send_json(401, {"error": "unauthorized"})

        key = self.headers.get("Idempotency-Key", "")
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except (ValueError, UnicodeDecodeError):
            return self.send_json(400, {"error": "invalid_request"})
        if (not key.strip() or not isinstance(payload, dict)
                or any(not isinstance(payload.get(field), str) or not payload[field].strip()
                       for field in ("order_id", "item_id"))):
            return self.send_json(400, {"error": "invalid_request"})

        # Derive the fake URL from the key so retries always get the same label.
        return self.send_json(201, {
            "label_url": f"https://mock-labels.example.test/{quote(key, safe='')}.pdf",
        })

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def mock_label_provider():
    # Port 0 selects an available port, so evals need no separate server setup.
    with HTTPServer(("127.0.0.1", 0), MockLabelHandler) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/labels"
        finally:
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    # Bind only to localhost; run this mock in a separate terminal.
    with HTTPServer(("127.0.0.1", 8765), MockLabelHandler) as server:
        print("Mock label API: http://127.0.0.1:8765/labels (token: mock-token)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
