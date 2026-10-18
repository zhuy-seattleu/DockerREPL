"""Step 3: length-prefixed JSON messages over TCP (comms.py)."""

import json
import socket
import struct
import threading
from collections.abc import Iterator
from contextlib import contextmanager
import pytest

from dockerrepl import comms
from dockerrepl.comms import (
    LMRequest,
    LMResponse,
    send_lm_request,
    send_lm_request_batched,
    socket_recv,
    socket_request,
    socket_send,
)

# ---------------------------------------------------------------- helpers


def frame(data: dict) -> bytes:
    """The exact bytes socket_send puts on the wire for `data`."""
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return struct.pack(">I", len(payload)) + payload


class ChunkySocket:
    """A fake socket whose recv() returns at most one byte at a time."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def recv(self, n: int) -> bytes:
        chunk, self._data = self._data[:1], self._data[1:]
        return chunk


class ShortWriteSocket:
    """A fake socket whose send() accepts at most 1000 bytes per call, like a busy network."""

    def __init__(self) -> None:
        self.written = bytearray()

    def send(self, data: bytes) -> int:
        self.written += data[:1000]
        return min(len(data), 1000)

    def sendall(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[self.send(bytes(view)):]


@contextmanager
def one_shot_server(
    reply: dict | None = None, raw: bytes | None = None, hold: bool = False
) -> Iterator[tuple[tuple[str, int], list[dict]]]:
    """A server on 127.0.0.1 that accepts one connection and reads one request.

    It then sends `raw` bytes if given, else `reply` as a message if given, else
    nothing, and closes. With `hold=True` it waits without replying until the
    test ends. Yields (address, list of requests received).
    """
    received: list[dict] = []
    release = threading.Event()
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(5)

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            received.append(socket_recv(conn))
            if hold:
                release.wait(5)
            elif raw is not None:
                conn.sendall(raw)
            elif reply is not None:
                socket_send(conn, reply)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname(), received
    finally:
        release.set()
        thread.join(5)
        listener.close()


def closed_port_address() -> tuple[str, int]:
    """An address nothing is listening on: bind a free port, then close it."""
    with socket.create_server(("127.0.0.1", 0)) as s:
        return s.getsockname()


# ---------------------------------------------------------------- dataclasses


def test_lm_request_roundtrip_single() -> None:
    r = LMRequest(prompt="hello", model="m")
    assert LMRequest.from_dict(r.to_dict()) == r
    assert not r.is_batched


def test_lm_request_roundtrip_batched() -> None:
    r = LMRequest(prompts=["a", "b"])
    assert LMRequest.from_dict(r.to_dict()) == r
    assert r.is_batched


def test_empty_prompts_list_is_not_batched() -> None:
    assert not LMRequest(prompts=[]).is_batched


def test_lm_request_to_dict_omits_none() -> None:
    assert LMRequest(prompt="p").to_dict() == {"prompt": "p"}


def test_lm_request_dict_prompt() -> None:
    r = LMRequest(prompt={"role": "user", "content": "hi"})
    assert LMRequest.from_dict(r.to_dict()) == r


def test_lm_response_roundtrip_nested() -> None:
    r = LMResponse(responses=[LMResponse(response="ok"), LMResponse(error="boom")])
    assert LMResponse.from_dict(r.to_dict()) == r
    assert LMResponse.from_dict(json.loads(json.dumps(r.to_dict()))) == r  # survives real JSON


def test_lm_response_success_flag() -> None:
    assert LMResponse(response="x").success
    assert not LMResponse(error="bad").success


# ---------------------------------------------------------------- framing


def test_send_recv_roundtrip() -> None:
    a, b = socket.socketpair()
    with a, b:
        message = {"prompt": "hi", "n": 3, "nested": {"ok": True}}
        socket_send(a, message)
        assert socket_recv(b) == message


def test_length_prefix_counts_bytes() -> None:
    message = {"t": "é"}
    a, b = socket.socketpair()
    with a, b:
        socket_send(a, message)
        (length,) = struct.unpack(">I", b.recv(4))
        payload = json.dumps(message, ensure_ascii=False)
        assert length == len(payload.encode("utf-8"))
        assert length != len(payload)  # "é" is 1 character but 2 bytes


def test_non_ascii_roundtrip() -> None:
    a, b = socket.socketpair()
    with a, b:
        message = {"text": "naïve café — 你好 🙂"}
        socket_send(a, message)
        assert socket_recv(b) == message


def test_large_message() -> None:
    message = {"big": "x" * 5_000_000}
    a, b = socket.socketpair()
    with a, b:
        sender = threading.Thread(target=socket_send, args=(a, message))  # sendall blocks until read
        sender.start()
        assert socket_recv(b) == message
        sender.join(5)


def test_partial_writes_are_completed() -> None:
    message = {"text": "y" * 5000}
    sock = ShortWriteSocket()
    socket_send(sock, message)  # type: ignore[arg-type]
    assert bytes(sock.written) == frame(message)  # every byte sent, not just the first 1000


def test_two_messages_back_to_back() -> None:
    a, b = socket.socketpair()
    with a, b:
        socket_send(a, {"n": 1})
        socket_send(a, {"n": 2})
        assert socket_recv(b) == {"n": 1}
        assert socket_recv(b) == {"n": 2}


def test_partial_reads_are_reassembled() -> None:
    message = {"prompt": "one byte at a time, header included"}
    assert socket_recv(ChunkySocket(frame(message))) == message  # type: ignore[arg-type]


def test_closed_before_header_returns_empty() -> None:
    a, b = socket.socketpair()
    with b:
        a.close()
        assert socket_recv(b) == {}


def test_closed_during_header_raises() -> None:
    with pytest.raises(ConnectionError, match="header"):
        socket_recv(ChunkySocket(b"\x00\x00"))  # type: ignore[arg-type]


def test_closed_mid_payload_raises() -> None:
    data = struct.pack(">I", 100) + b"x" * 10
    with pytest.raises(ConnectionError, match="10 of 100"):
        socket_recv(ChunkySocket(data))  # type: ignore[arg-type]


def test_oversized_incoming_message_rejected() -> None:
    header = struct.pack(">I", comms.MAX_MESSAGE_BYTES + 1)  # claims ~64 MiB, sends nothing
    with pytest.raises(ValueError, match="limit"):
        socket_recv(ChunkySocket(header))  # type: ignore[arg-type]


def test_oversized_outgoing_message_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(comms, "MAX_MESSAGE_BYTES", 10)
    a, b = socket.socketpair()
    with a, b, pytest.raises(ValueError, match="limit"):
        socket_send(a, {"text": "far more than ten bytes"})


def test_non_object_payload_rejected() -> None:
    payload = json.dumps([1, 2, 3]).encode()
    with pytest.raises(ValueError, match="JSON object"):
        socket_recv(ChunkySocket(struct.pack(">I", len(payload)) + payload))  # type: ignore[arg-type]


# ---------------------------------------------------------------- request helpers


def test_socket_request_against_server() -> None:
    with one_shot_server(reply={"ok": 1}) as (address, received):
        assert socket_request(address, {"ping": True}) == {"ok": 1}
    assert received == [{"ping": True}]


def test_send_lm_request_single() -> None:
    with one_shot_server(reply={"response": "pong"}) as (address, received):
        result = send_lm_request(address, LMRequest(prompt="ping", model="m"))
    assert result == LMResponse(response="pong")
    assert received == [{"prompt": "ping", "model": "m"}]


def test_send_lm_request_connection_refused() -> None:
    result = send_lm_request(closed_port_address(), LMRequest(prompt="x"))
    assert not result.success
    assert "Request failed" in result.error


def test_send_lm_request_server_closes_without_reply() -> None:
    with one_shot_server() as (address, _):
        result = send_lm_request(address, LMRequest(prompt="x"))
    assert not result.success
    assert "without a reply" in result.error


def test_send_lm_request_invalid_reply() -> None:
    with one_shot_server(raw=struct.pack(">I", 5) + b"{oops") as (address, _):
        result = send_lm_request(address, LMRequest(prompt="x"))
    assert not result.success


def test_send_lm_request_times_out() -> None:
    with one_shot_server(hold=True) as (address, _):
        result = send_lm_request(address, LMRequest(prompt="x"), timeout=0.5)
    assert not result.success
    assert "timed out" in result.error


def test_send_lm_request_batched_unpacks() -> None:
    reply = LMResponse(responses=[LMResponse(response="A"), LMResponse(error="bad"), LMResponse(response="C")])
    with one_shot_server(reply=reply.to_dict()) as (address, received):
        results = send_lm_request_batched(address, ["a", "b", "c"], model="m")
    assert [r.response for r in results] == ["A", None, "C"]
    assert [r.success for r in results] == [True, False, True]
    assert received == [{"prompts": ["a", "b", "c"], "model": "m"}]


def test_send_lm_request_batched_failure_fills_all() -> None:
    results = send_lm_request_batched(closed_port_address(), ["a", "b", "c"])
    assert len(results) == 3
    assert all(not r.success for r in results)


def test_send_lm_request_batched_wrong_count() -> None:
    reply = LMResponse(responses=[LMResponse(response="only one")])
    with one_shot_server(reply=reply.to_dict()) as (address, _):
        results = send_lm_request_batched(address, ["a", "b"])
    assert len(results) == 2
    assert all("expected 2 responses, got 1" in r.error for r in results)


def test_send_lm_request_batched_missing_responses() -> None:
    with one_shot_server(reply={"response": "not a batch"}) as (address, _):
        results = send_lm_request_batched(address, ["a", "b"])
    assert all(not r.success for r in results)


def test_send_helpers_never_raise() -> None:
    bad_address = ("256.0.0.1", 1)  # not a valid IP address at all
    assert not send_lm_request(bad_address, LMRequest(prompt="x"), timeout=1).success
    assert all(not r.success for r in send_lm_request_batched(bad_address, ["a"], timeout=1))
