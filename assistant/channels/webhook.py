"""A tiny stdlib HTTP server that receives Twilio webhook POSTs."""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qsl

from assistant.channels.base import InboundMessage

Handler = Callable[[str, dict[str, str], dict[str, str]], tuple[int, str, InboundMessage | None]]
OnMessage = Callable[[InboundMessage], Any]


class WebhookServer:
    """Serves `POST {path}` (form-encoded) and `GET /healthz` on 0.0.0.0:{port}.

    Every accepted POST is passed to `handler(public_url, headers, form)`; when it
    returns a message, `on_message(msg)` is called from the server thread.
    """

    def __init__(
        self,
        port: int,
        path: str,
        public_url: str,
        handler: Handler,
        on_message: OnMessage,
        host: str = "0.0.0.0",
        log: logging.Logger | None = None,
    ) -> None:
        self.port = port
        self.path = path if path.startswith("/") else f"/{path}"
        self.public_url = public_url
        self.handler = handler
        self.on_message = on_message
        self.host = host
        self.log = log or logging.getLogger("assistant.webhook")
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _make_request_handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class RequestHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 - quiet
                server.log.debug("webhook: " + fmt, *args)

            def _reply(self, status: int, body: str, content_type: str = "text/plain") -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                if self.path == "/healthz":
                    self._reply(200, "ok")
                else:
                    self._reply(404, "not found")

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                if self.path.split("?", 1)[0] != server.path:
                    self._reply(404, "not found")
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b""
                form = dict(parse_qsl(raw.decode("utf-8", errors="replace"), keep_blank_values=True))
                headers = {k: v for k, v in self.headers.items()}
                try:
                    status, body, message = server.handler(server.public_url, headers, form)
                except Exception:  # noqa: BLE001 - never crash the listener
                    server.log.exception("webhook handler failed")
                    self._reply(500, "error")
                    return
                content_type = "text/xml" if body.lstrip().startswith("<") else "text/plain"
                self._reply(status, body, content_type)
                if message is not None:
                    try:
                        server.on_message(message)
                    except Exception:  # noqa: BLE001
                        server.log.exception("webhook on_message failed")

        return RequestHandler

    def start(self) -> None:
        self._server = ThreadingHTTPServer((self.host, self.port), self._make_request_handler())
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="webhook", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
