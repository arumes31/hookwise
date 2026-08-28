"""HTTP-level integration coverage for the local Ollama client."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

from hookwise.utils import call_llm


class _OllamaHandler(BaseHTTPRequestHandler):
    attempts: ClassVar[int] = 0
    request_payload: ClassVar[dict[str, Any] | None] = None

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).attempts += 1
        content_length = int(self.headers["Content-Length"])
        type(self).request_payload = json.loads(self.rfile.read(content_length))

        if type(self).attempts == 1:
            self.send_response(503)
            self.end_headers()
            return

        response = json.dumps({"response": "  bounded retry succeeded  "}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format: str, *args: object) -> None:
        """Keep the test output free of the server's access log."""


def test_call_llm_retries_transient_http_failure(monkeypatch: Any) -> None:
    _OllamaHandler.attempts = 0
    _OllamaHandler.request_payload = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        monkeypatch.setenv("OLLAMA_HOST", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setenv("LLM_CONNECT_TIMEOUT", "1")
        monkeypatch.setenv("LLM_TIMEOUT", "1")

        assert call_llm("integration prompt", "integration system") == "bounded retry succeeded"
        assert _OllamaHandler.attempts == 2
        assert _OllamaHandler.request_payload is not None
        assert _OllamaHandler.request_payload["prompt"] == "integration prompt"
        assert _OllamaHandler.request_payload["system"] == "integration system"
        assert _OllamaHandler.request_payload["stream"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
