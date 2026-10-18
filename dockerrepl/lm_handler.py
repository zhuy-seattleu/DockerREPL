"""LMHandler: a threaded TCP server that answers LMRequests using a BaseLM."""

import socketserver
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from dockerrepl.clients.base_lm import BaseLM, Messages, Prompt
from dockerrepl.comms import LMRequest, LMResponse, socket_recv, socket_send
from dockerrepl.constants import DEFAULT_TIMEOUT

# How often serve_forever() checks for a shutdown request; small, so stop() is quick.
POLL_INTERVAL: float = 0.05


def _error_text(e: Exception) -> str:
    """Describe an exception, including its type, even if its message is empty."""
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


class LMRequestHandler(socketserver.StreamRequestHandler):
    """Handles one connection: read one LMRequest, answer it, close."""

    # socketserver calls settimeout() on each connection with this value, so a
    # client that stalls mid-message can't hold this thread forever.
    timeout: float = DEFAULT_TIMEOUT

    def handle(self) -> None:
        try:
            data = socket_recv(self.connection)
        except OSError:
            return  # timed out or dropped mid-message: nobody to answer
        except ValueError as e:
            self._reply(LMResponse(error=f"Bad request: {e}"))  # client still connected
            return
        if not data:
            return  # the client closed without sending anything

        request = LMRequest.from_dict(data)
        client = self.server.lm_handler.client  # type: ignore[attr-defined]
        if request.is_batched:
            response = self._batched(client, request.prompts or [])
        elif request.prompt is not None:
            response = self._single(client, request.prompt)
        else:
            response = LMResponse(error="Bad request: missing 'prompt' or 'prompts'")
        self._reply(response)

    @staticmethod
    def _single(client: BaseLM, prompt: Prompt) -> LMResponse:
        try:
            return LMResponse(response=client.completion(prompt))
        except Exception as e:
            return LMResponse(error=_error_text(e))

    @classmethod
    def _batched(cls, client: BaseLM, prompts: list[Prompt]) -> LMResponse:
        # map() returns results in input order; each prompt's failure stays in its slot.
        workers = min(len(prompts), client.max_concurrent)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda p: cls._single(client, p), prompts))
        return LMResponse(responses=results)

    def _reply(self, response: LMResponse) -> None:
        try:
            socket_send(self.connection, response.to_dict())
        except OSError:
            pass  # the client already went away


class ThreadingLMServer(socketserver.ThreadingTCPServer):
    """A TCP server that handles each connection in its own daemon thread."""

    daemon_threads = True
    allow_reuse_address = True
    # Connections allowed to wait to be accepted. The socketserver default of 5 is
    # too small: when more clients connect at once, the extra ones are dropped and
    # only retry about a second later.
    request_queue_size = 128
    lm_handler: "LMHandler"  # set by LMHandler.start()


class LMHandler:
    """Serves LMRequests for one round, using `client` for every completion.

    The `model` field of a request is accepted but ignored: this MVP has a
    single LM client.
    """

    def __init__(self, client: BaseLM, host: str = "127.0.0.1", port: int = 0) -> None:
        self.client: BaseLM = client
        self.host: str = host
        self._port: int = port
        self._server: ThreadingLMServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        """(host, port) clients connect to; the real port once started."""
        if self._server is None:
            raise RuntimeError("LMHandler is not running; call start() first")
        return (self.host, self._server.server_address[1])

    def start(self) -> tuple[str, int]:
        """Start serving in a background thread. Calling it again does nothing."""
        if self._server is None:
            server = ThreadingLMServer((self.host, self._port), LMRequestHandler)
            server.lm_handler = self
            self._thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": POLL_INTERVAL}, daemon=True
            )
            self._server = server
            self._thread.start()
        return self.address

    def stop(self) -> None:
        """Stop serving and free the port. Calling it again does nothing."""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join()
        self._server = None
        self._thread = None

    def completion(self, prompt: Prompt | Messages, model: str | None = None) -> str:
        """Direct call used by the Client; same concurrency cap as remote requests."""
        return self.client.completion(prompt)

    def __enter__(self) -> "LMHandler":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
