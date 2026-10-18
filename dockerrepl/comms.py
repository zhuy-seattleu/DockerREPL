"""Messages between LMProxyHandler and LMHandler: length-prefixed JSON over TCP.

Each message on the wire is a 4-byte big-endian unsigned length, followed by
exactly that many bytes of UTF-8 encoded JSON. One TCP connection carries one
request and one reply.
"""

import json
import socket
import struct
from dataclasses import dataclass
from typing import Any

from dockerrepl.clients.base_lm import Prompt
from dockerrepl.constants import DEFAULT_TIMEOUT

HEADER_FORMAT = ">I"  # big-endian unsigned 32-bit integer
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 4 bytes
MAX_MESSAGE_BYTES = 64 * 1024 * 1024  # refuse anything larger (64 MiB)


@dataclass
class LMRequest:
    """A request for one prompt (`prompt`) or a batch of prompts (`prompts`)."""

    prompt: Prompt | None = None
    prompts: list[Prompt] | None = None
    model: str | None = None

    @property
    def is_batched(self) -> bool:
        """True if this request carries a non-empty list of prompts."""
        return bool(self.prompts)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready dict, leaving out fields that are None."""
        fields = {"prompt": self.prompt, "prompts": self.prompts, "model": self.model}
        return {key: value for key, value in fields.items() if value is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LMRequest":
        """Build a request from a dict; missing keys become None."""
        return cls(prompt=data.get("prompt"), prompts=data.get("prompts"), model=data.get("model"))


@dataclass
class LMResponse:
    """A reply: `response` for one prompt, `responses` (one each) for a batch, or `error`."""

    error: str | None = None
    response: str | None = None
    responses: list["LMResponse"] | None = None

    @property
    def success(self) -> bool:
        """True if no error was reported."""
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ready dict, leaving out fields that are None."""
        data: dict[str, Any] = {}
        if self.error is not None:
            data["error"] = self.error
        if self.response is not None:
            data["response"] = self.response
        if self.responses is not None:
            data["responses"] = [r.to_dict() for r in self.responses]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LMResponse":
        """Build a response from a dict, including nested batch responses."""
        nested = data.get("responses")
        return cls(
            error=data.get("error"),
            response=data.get("response"),
            responses=[cls.from_dict(r) for r in nested] if nested is not None else None,
        )


def socket_send(sock: socket.socket, data: dict) -> None:
    """Send one message: the 4-byte length of the UTF-8 JSON payload, then the payload."""
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError(f"message is {len(payload)} bytes; the limit is {MAX_MESSAGE_BYTES}")
    sock.sendall(struct.pack(HEADER_FORMAT, len(payload)) + payload)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read until `n` bytes have arrived or the peer closes; may return fewer."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), 65536))
        if not chunk:  # peer closed the connection
            break
        buf += chunk
    return bytes(buf)


def socket_recv(sock: socket.socket) -> dict:
    """Receive one message. Returns {} if the peer closed before sending anything.

    Raises ConnectionError if the peer closes part-way through a message, and
    ValueError if the message is too large or isn't a JSON object.
    """
    header = _recv_exact(sock, HEADER_SIZE)
    if not header:
        return {}
    if len(header) < HEADER_SIZE:
        raise ConnectionError("connection closed during the message header")
    (length,) = struct.unpack(HEADER_FORMAT, header)
    if length > MAX_MESSAGE_BYTES:
        raise ValueError(f"incoming message claims {length} bytes; the limit is {MAX_MESSAGE_BYTES}")
    payload = _recv_exact(sock, length)
    if len(payload) < length:
        raise ConnectionError(f"connection closed after {len(payload)} of {length} payload bytes")
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    return data


def socket_request(address: tuple[str, int], data: dict, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Open a new connection, send one message, read one reply, and close."""
    with socket.create_connection(address, timeout=timeout) as sock:
        socket_send(sock, data)
        return socket_recv(sock)


def send_lm_request(address: tuple[str, int], request: LMRequest, timeout: float = DEFAULT_TIMEOUT) -> LMResponse:
    """Send a request and return the reply. Never raises: failures become `LMResponse(error=...)`."""
    try:
        data = socket_request(address, request.to_dict(), timeout)
    except Exception as e:
        return LMResponse(error=f"Request failed: {e}")
    if not data:
        return LMResponse(error="Request failed: connection closed without a reply")
    return LMResponse.from_dict(data)


def send_lm_request_batched(
    address: tuple[str, int],
    prompts: list[Prompt],
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[LMResponse]:
    """Send all prompts in one request; return one LMResponse per prompt, in order.

    Never raises. If the request as a whole fails, every prompt gets the same error.
    """
    reply = send_lm_request(address, LMRequest(prompts=prompts, model=model), timeout)
    if not reply.success:
        return [LMResponse(error=reply.error) for _ in prompts]
    if reply.responses is None:
        return [LMResponse(error="Batched request failed: no responses in reply") for _ in prompts]
    if len(reply.responses) != len(prompts):
        error = f"Batched request failed: expected {len(prompts)} responses, got {len(reply.responses)}"
        return [LMResponse(error=error) for _ in prompts]
    return reply.responses
