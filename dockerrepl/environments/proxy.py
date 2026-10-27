"""LMProxyHandler: the HTTP server that `llm_query` in the container calls.

Each request is forwarded to the current LMHandler over TCP (step 3) and
answered with JSON. "Current" is read from the owner object (the
ContainerManager in step 8) at the start of every request, so the owner can
point the proxy at a new LMHandler each round without restarting it.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from dockerrepl.comms import MAX_MESSAGE_BYTES, LMRequest, send_lm_request, send_lm_request_batched
from dockerrepl.constants import DEFAULT_TIMEOUT


class BadRequest(Exception):
    """The request can't be served as sent; the message says why."""


class LMProxyHandler(BaseHTTPRequestHandler):
    """Handles one HTTP request. Use make_handler_class() to bind it to an owner."""

    manager: Any  # set on each subclass: any object with a .lm_handler_address attribute

    # Applies to reading the request and writing the reply, so a client that
    # stalls mid-request can't hold this thread forever.
    timeout: float = DEFAULT_TIMEOUT

    def do_POST(self) -> None:
        try:
            address = self.manager.lm_handler_address  # read once per request
            body = self._read_json()
            if self.path == "/llm_query":
                self._respond(200, self._llm_query(address, body))
            elif self.path == "/llm_query_batched":
                self._respond(200, self._llm_query_batched(address, body))
            else:
                self._respond(404, {"error": f"Not found: {self.path}"})
        except BadRequest as e:
            self._respond(400, {"error": f"Bad request: {e}"})
        except Exception as e:
            self._respond(500, {"error": f"Proxy handler error: {type(e).__name__}: {e}"})

    def _read_json(self) -> dict[str, Any]:
        length_text = self.headers.get("Content-Length")
        if length_text is None:
            raise BadRequest("missing Content-Length")
        try:
            length = int(length_text)
        except ValueError:
            raise BadRequest(f"invalid Content-Length {length_text!r}") from None
        if not 0 <= length <= MAX_MESSAGE_BYTES:
            raise BadRequest(f"body must be 0 to {MAX_MESSAGE_BYTES} bytes, not {length}")
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError as e:  # includes bad UTF-8 and bad JSON
            raise BadRequest(f"body is not valid JSON ({e})") from None
        if not isinstance(body, dict):
            raise BadRequest("body must be a JSON object")
        return body

    @staticmethod
    def _llm_query(address: tuple[str, int] | None, body: dict[str, Any]) -> dict[str, Any]:
        prompt = body.get("prompt")
        if not isinstance(prompt, (str, dict)):
            raise BadRequest("'prompt' must be a string or an object")
        if address is None:
            return {"error": "No LM handler configured"}
        reply = send_lm_request(address, LMRequest(prompt=prompt, model=body.get("model")))
        return {"response": reply.response} if reply.success else {"error": reply.error}

    @staticmethod
    def _llm_query_batched(address: tuple[str, int] | None, body: dict[str, Any]) -> dict[str, Any]:
        prompts = body.get("prompts")
        if not isinstance(prompts, list) or not all(isinstance(p, (str, dict)) for p in prompts):
            raise BadRequest("'prompts' must be a list of strings or objects")
        if address is None:
            return {"error": "No LM handler configured"}
        if not prompts:
            return {"responses": []}
        replies = send_lm_request_batched(address, prompts, body.get("model"))
        return {"responses": [r.response if r.success else f"Error: {r.error}" for r in replies]}

    def _respond(self, status: int, data: dict[str, Any]) -> None:
        payload = json.dumps(data).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass  # the caller gave up (timed out or disconnected)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # no line on stderr for every request


class ThreadingProxyServer(ThreadingHTTPServer):
    """An HTTP server that handles each request in its own daemon thread."""

    daemon_threads = True
    # Connections allowed to wait to be accepted. The default of 5 is too small:
    # when more callers connect at once, the extra ones only retry about a second later.
    request_queue_size = 128


def make_handler_class(manager: Any) -> type[LMProxyHandler]:
    """A handler class bound to `manager`. Holds a reference, so changes to it are seen at once."""
    return type("Handler", (LMProxyHandler,), {"manager": manager})


def start_proxy_server(
    manager: Any, host: str = "0.0.0.0", port: int = 0
) -> tuple[ThreadingProxyServer, threading.Thread]:
    """Start serving in a daemon thread. The real port is server.server_address[1].

    Stop it with server.shutdown() and then server.server_close().
    """
    server = ThreadingProxyServer((host, port), make_handler_class(manager))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    return server, thread
