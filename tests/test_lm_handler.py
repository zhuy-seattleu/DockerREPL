"""Step 4: LMHandler, a threaded TCP server answering LMRequests."""

import json
import socket
import struct
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from dockerrepl.clients import BaseLM, Messages, MockLM, Prompt
from dockerrepl.comms import LMRequest, send_lm_request, send_lm_request_batched
from dockerrepl.constants import DEFAULT_TIMEOUT
from dockerrepl.lm_handler import LMHandler, LMRequestHandler, ThreadingLMServer

# ---------------------------------------------------------------- test LMs


class SlowLM(BaseLM):
    """Takes 0.2 s per call, like a real backend."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("slow", **kwargs)

    def _completion(self, prompt: Prompt | Messages) -> str:
        time.sleep(0.2)
        return "slow"


class PickyLM(BaseLM):
    """Fails on the prompt "boom"; otherwise answers "ok:<prompt>"."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("picky", **kwargs)

    def _completion(self, prompt: Prompt | Messages) -> str:
        if prompt == "boom":
            raise RuntimeError("cannot answer boom")
        return f"ok:{prompt}"


class SilentFailLM(BaseLM):
    """Raises an exception with an empty message."""

    def _completion(self, prompt: Prompt | Messages) -> str:
        raise ValueError()


@pytest.fixture(autouse=True)
def no_unhandled_server_errors(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail any test in which an exception escaped LMRequestHandler.handle().

    socketserver calls handle_error() for those, which only prints a traceback,
    so without this check such a bug would go unnoticed.
    """
    errors: list[str] = []
    monkeypatch.setattr(
        ThreadingLMServer, "handle_error", lambda self, request, address: errors.append("error")
    )
    yield
    assert not errors, "an exception escaped LMRequestHandler.handle()"


@pytest.fixture
def handler() -> Iterator[LMHandler]:
    with LMHandler(MockLM(["first scripted reply"])) as h:
        yield h


def run_in_threads(n: int, target: Any) -> list[Any]:
    """Run target() in n daemon threads that start together; return the results."""
    barrier = threading.Barrier(n)
    results: list[Any] = [None] * n

    def worker(i: int) -> None:
        barrier.wait()
        results[i] = target()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads), "threads did not finish"
    return results


# ---------------------------------------------------------------- lifecycle


def test_start_assigns_real_port() -> None:
    h = LMHandler(MockLM())
    try:
        host, port = h.start()
        assert host == "127.0.0.1"
        assert port > 0
        assert h.address == (host, port)
    finally:
        h.stop()


def test_address_before_start_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="start"):
        LMHandler(MockLM()).address


def test_start_twice_keeps_one_server() -> None:
    with LMHandler(MockLM()) as h:
        assert h.start() == h.address  # second start: same address, no new server


def test_context_manager_starts_and_stops() -> None:
    with LMHandler(MockLM()) as h:
        address = h.address
        assert send_lm_request(address, LMRequest(prompt="hi")).response == "Mock: hi"
    with pytest.raises(OSError):
        socket.create_connection(address, timeout=2).close()


def test_stop_is_idempotent() -> None:
    h = LMHandler(MockLM())
    h.start()
    h.stop()
    h.stop()


def test_stop_is_quick() -> None:
    h = LMHandler(MockLM())
    h.start()
    started = time.perf_counter()
    h.stop()
    assert time.perf_counter() - started < 0.4  # the default 0.5 s poll interval would fail this


def test_two_handlers_coexist() -> None:
    with LMHandler(PickyLM()) as a, LMHandler(MockLM()) as b:
        assert a.address[1] != b.address[1]
        assert send_lm_request(a.address, LMRequest(prompt="x")).response == "ok:x"
        assert send_lm_request(b.address, LMRequest(prompt="x")).response == "Mock: x"


# ---------------------------------------------------------------- requests


def test_single_request(handler: LMHandler) -> None:
    result = send_lm_request(handler.address, LMRequest(prompt="hi"))
    assert result.success
    assert result.response == "Mock: hi"


def test_dict_prompt_request(handler: LMHandler) -> None:
    result = send_lm_request(handler.address, LMRequest(prompt={"role": "user", "content": "hi"}))
    assert result.response.startswith("Mock: ")


def test_batched_request_preserves_order(handler: LMHandler) -> None:
    prompts = [f"p{i}" for i in range(5)]
    results = send_lm_request_batched(handler.address, prompts)
    assert [r.response for r in results] == [f"Mock: p{i}" for i in range(5)]


def test_batched_isolates_failures() -> None:
    with LMHandler(PickyLM()) as h:
        results = send_lm_request_batched(h.address, ["a", "boom", "c"])
    assert [r.success for r in results] == [True, False, True]
    assert results[0].response == "ok:a"
    assert "cannot answer boom" in results[1].error


def test_single_request_error_is_reported() -> None:
    with LMHandler(PickyLM()) as h:
        result = send_lm_request(h.address, LMRequest(prompt="boom"))
        assert not result.success
        assert result.error == "RuntimeError: cannot answer boom"
        assert send_lm_request(h.address, LMRequest(prompt="fine")).response == "ok:fine"


def test_error_with_empty_message_still_names_the_type() -> None:
    with LMHandler(SilentFailLM("s")) as h:
        assert send_lm_request(h.address, LMRequest(prompt="x")).error == "ValueError"


def test_missing_prompt_is_an_error(handler: LMHandler) -> None:
    result = send_lm_request(handler.address, LMRequest(model="m"))
    assert not result.success
    assert "missing" in result.error


def test_empty_batch_is_an_error(handler: LMHandler) -> None:
    assert not send_lm_request(handler.address, LMRequest(prompts=[])).success


def test_direct_completion_uses_client(handler: LMHandler) -> None:
    assert handler.completion([{"role": "user", "content": "x"}]) == "first scripted reply"


# ---------------------------------------------------------------- concurrency


def test_concurrent_clients() -> None:
    with LMHandler(SlowLM(max_concurrent=10)) as h:
        started = time.perf_counter()
        results = run_in_threads(10, lambda: send_lm_request(h.address, LMRequest(prompt="p")))
        elapsed = time.perf_counter() - started
    assert all(r.response == "slow" for r in results)
    assert elapsed < 1.0  # in parallel ~0.2 s; one at a time would be 2.0 s


def test_batched_runs_in_parallel() -> None:
    with LMHandler(SlowLM(max_concurrent=5)) as h:
        started = time.perf_counter()
        results = send_lm_request_batched(h.address, ["a"] * 5)
        elapsed = time.perf_counter() - started
    assert [r.response for r in results] == ["slow"] * 5
    assert elapsed < 0.6  # in parallel ~0.2 s; one at a time would be 1.0 s


def test_batched_respects_cap() -> None:
    with LMHandler(SlowLM(max_concurrent=1)) as h:
        started = time.perf_counter()
        results = send_lm_request_batched(h.address, ["a", "b", "c"])
        elapsed = time.perf_counter() - started
    assert [r.response for r in results] == ["slow"] * 3
    assert elapsed >= 0.6  # the cap of 1 forces one call at a time


# ---------------------------------------------------------------- bad clients


def test_client_disconnect_does_not_crash_server(handler: LMHandler) -> None:
    with socket.create_connection(handler.address, timeout=2) as s:
        s.sendall(struct.pack(">I", 100) + b"only part of the message")
    assert send_lm_request(handler.address, LMRequest(prompt="after")).response == "Mock: after"


def test_handler_has_a_timeout_by_default() -> None:
    # StreamRequestHandler's own default is None, which waits forever.
    assert LMRequestHandler.timeout == DEFAULT_TIMEOUT


def test_stalled_client_times_out(handler: LMHandler, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LMRequestHandler, "timeout", 0.5)
    with socket.create_connection(handler.address, timeout=5) as stalled:
        stalled.sendall(b"\x00\x00")  # half a header, then nothing
        # Meanwhile another client is served normally.
        assert send_lm_request(handler.address, LMRequest(prompt="other")).response == "Mock: other"
        started = time.perf_counter()
        assert stalled.recv(1) == b""  # the server gave up and closed this connection
        assert time.perf_counter() - started < 4


def test_malformed_request_gets_error_reply(handler: LMHandler) -> None:
    payload = b"{not json"
    with socket.create_connection(handler.address, timeout=5) as s:
        s.sendall(struct.pack(">I", len(payload)) + payload)
        header = s.recv(4)
        body = s.recv(struct.unpack(">I", header)[0])
    reply = json.loads(body)
    assert reply["error"].startswith("Bad request")
    assert send_lm_request(handler.address, LMRequest(prompt="ok")).response == "Mock: ok"
