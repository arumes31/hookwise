"""Minimal ConnectWise Manage API stand-in for the integration stack.

Ticket creation succeeds only for companies in KNOWN_COMPANIES, like a real
instance that does not know the requested company. Every request is recorded
and listed at GET /__requests so tests can assert what HookWise sent.
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

KNOWN_COMPANIES = {"ACME"}

# Write paths HookWise uses; anything else is answered with 404 so a wrong API
# path fails the test instead of looking like a successful write.
TICKETS_PATH = re.compile(r"/service/tickets/?$")
TICKET_PATH = re.compile(r"/service/tickets/\d+/?$")
NOTES_PATH = re.compile(r"/service/tickets/\d+/notes/?$")

_lock = threading.Lock()
_recorded: list[dict[str, Any]] = []
_next_ticket_id = 9000


class Handler(BaseHTTPRequestHandler):
    def _read_body(self) -> Any:
        """Return the JSON request body, or the raw text if it is not JSON."""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return raw.decode(errors="replace")

    def _reply(self, code: int, body: Any, request_body: Any = None) -> None:
        """Record the request and send a JSON response."""
        with _lock:
            _recorded.append({"method": self.command, "path": self.path, "status": code, "body": request_body})
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        """Serve the request log, a ticket lookup or an empty list."""
        if self.path == "/__requests":
            with _lock:
                raw = json.dumps(_recorded).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if "/service/tickets/" in self.path:
            ticket_id = self.path.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            self._reply(200, {"id": int(ticket_id) if ticket_id.isdigit() else 0, "closedFlag": False})
            return
        self._reply(200, [])

    def do_POST(self) -> None:
        """Add notes, create tickets for known companies, reject everything else."""
        global _next_ticket_id
        body = self._read_body()
        path = self.path.split("?")[0]
        if NOTES_PATH.search(path):
            self._reply(200, {"id": 1}, body)
            return
        if not TICKETS_PATH.search(path):
            self._reply(404, {"code": "NotFound", "message": f"mock: unsupported POST {path}"}, body)
            return
        company = ((body or {}).get("company") or {}).get("identifier")
        if company in KNOWN_COMPANIES:
            with _lock:
                _next_ticket_id += 1
                ticket_id = _next_ticket_id
            self._reply(201, {"id": ticket_id, "summary": body.get("summary")}, body)
            return
        error = {
            "code": "InvalidObject",
            "message": "ticket object is invalid",
            "errors": [
                {
                    "code": "InvalidObject",
                    "resource": "ticket",
                    "field": "company",
                    "message": f"Company identifier '{company}' was not found.",
                }
            ],
        }
        self._reply(400, error, body)

    def do_PATCH(self) -> None:
        """Accept ticket updates such as closing; reject any other path."""
        body = self._read_body()
        path = self.path.split("?")[0]
        if TICKET_PATH.search(path):
            self._reply(200, {"id": 1}, body)
            return
        self._reply(404, {"code": "NotFound", "message": f"mock: unsupported PATCH {path}"}, body)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default per-request stderr logging."""
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
