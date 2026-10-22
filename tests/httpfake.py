"""A real local HTTP server that scripts JSON responses and records requests."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Responder = Callable[[dict[str, Any]], tuple[int, dict[str, Any], dict[str, str]]]


class FakeServer:
    def __init__(self, responses: list[tuple[int, dict[str, Any]] | Responder]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                record = {"path": self.path, "headers": {k.lower(): v for k, v in
                                                         self.headers.items()}, "body": body}
                fake.requests.append(record)
                step = fake.responses.pop(0) if fake.responses else (500, {"error": "empty"})
                if callable(step):
                    status, payload, headers = step(record)
                else:
                    status, payload = step
                    headers = {}
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> FakeServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
