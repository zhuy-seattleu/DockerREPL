"""Step 2: BaseLM (interface + concurrency cap) and MockLM (scripted replies)."""

import threading
import time
from typing import Any

import pytest

from dockerrepl.clients import MOCK_FINAL_REPLY, BaseLM, Messages, MockLM, Prompt
from dockerrepl.constants import DEFAULT_TIMEOUT
from dockerrepl.parsing import find_code_blocks

ROOT: Messages = [{"role": "user", "content": "What is 2 + 2?"}]  # a root conversation


class EchoLM(BaseLM):
    """Smallest possible subclass: returns the prompt as text."""

    def _completion(self, prompt: Prompt | Messages) -> str:
        return str(prompt)


class SlowLM(BaseLM):
    """Records how many calls are inside _completion at the same time."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("slow", **kwargs)
        self.in_flight = 0
        self.peak = 0
        self._count_lock = threading.Lock()

    def _completion(self, prompt: Prompt | Messages) -> str:
        with self._count_lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        time.sleep(0.05)  # simulated backend latency
        with self._count_lock:
            self.in_flight -= 1
        return "ok"


class FailingLM(BaseLM):
    """Every call raises, like a backend that is down."""

    def _completion(self, prompt: Prompt | Messages) -> str:
        raise RuntimeError("backend down")


def run_in_threads(n: int, target: Any, timeout: float = 10) -> list[Any]:
    """Run `target()` in n threads that all start together; return their results.

    Threads are daemons, so a thread stuck forever (a bug) fails the test
    instead of keeping the test process alive.
    """
    barrier = threading.Barrier(n)
    results: list[Any] = [None] * n

    def worker(i: int) -> None:
        barrier.wait()
        results[i] = target()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
    assert not any(t.is_alive() for t in threads), "threads did not finish"
    return results


# ---------------------------------------------------------------- BaseLM


def test_base_lm_is_abstract() -> None:
    with pytest.raises(TypeError):
        BaseLM("x")  # type: ignore[abstract]


def test_base_lm_stores_options() -> None:
    lm = EchoLM("m", timeout=12.5, sampling_args={"temperature": 0.2}, api_key="k")
    assert lm.model_name == "m"
    assert lm.timeout == 12.5
    assert lm.sampling_args == {"temperature": 0.2}
    assert lm.kwargs == {"api_key": "k"}


def test_defaults() -> None:
    lm = EchoLM("m")
    assert lm.timeout == DEFAULT_TIMEOUT
    assert lm.sampling_args == {}  # None becomes an empty dict
    assert lm.max_concurrent == 8


def test_max_concurrent_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_concurrent"):
        EchoLM("m", max_concurrent=0)


def test_overriding_completion_is_refused() -> None:
    with pytest.raises(TypeError, match="_completion"):

        class Sneaky(BaseLM):  # noqa: F841 - defining the class is the test
            def completion(self, prompt: Prompt | Messages) -> str:
                return "bypasses the cap"

            def _completion(self, prompt: Prompt | Messages) -> str:
                return ""


def test_semaphore_caps_concurrency() -> None:
    lm = SlowLM(max_concurrent=2)
    results = run_in_threads(10, lambda: lm.completion("p"))
    assert results == ["ok"] * 10
    assert lm.peak == 2  # never more than 2, and the cap was actually reached


def test_calls_run_in_parallel_up_to_the_cap() -> None:
    lm = SlowLM(max_concurrent=5)
    run_in_threads(5, lambda: lm.completion("p"))
    assert lm.peak == 5


def test_slot_released_after_exception() -> None:
    lm = FailingLM("f", max_concurrent=1)
    for _ in range(2):  # the second call would block forever if the slot leaked
        result = run_in_threads(1, lambda: pytest.raises(RuntimeError, lm.completion, "p"), timeout=2)
        assert result[0] is not None


# ---------------------------------------------------------------- MockLM


def test_mock_returns_scripted_in_order() -> None:
    lm = MockLM(["r1", "r2"])
    assert lm.completion(ROOT) == "r1"
    assert lm.completion(ROOT) == "r2"


def test_mock_falls_back_when_script_exhausted() -> None:
    lm = MockLM(["r1"])
    lm.completion(ROOT)
    assert lm.completion(ROOT) == MOCK_FINAL_REPLY
    assert lm.completion(ROOT) == MOCK_FINAL_REPLY


def test_empty_script_goes_straight_to_final_reply() -> None:
    assert MockLM().completion(ROOT) == MOCK_FINAL_REPLY


def test_final_reply_contains_ready_block() -> None:
    blocks = find_code_blocks(MOCK_FINAL_REPLY)
    assert len(blocks) == 1
    assert 'answer["ready"] = True' in blocks[0]
    compile(blocks[0], "<repl>", "exec")


def test_subquery_echo_does_not_advance_cursor() -> None:
    lm = MockLM(["r1", "r2"])
    assert lm.completion("hello") == "Mock: hello"
    assert lm.completion(ROOT) == "r1"  # the sub-query did not use up "r1"


def test_dict_prompt_is_subquery() -> None:
    lm = MockLM(["r1"])
    reply = lm.completion({"role": "user", "content": "hi"})
    assert reply.startswith("Mock: ")
    assert lm.completion(ROOT) == "r1"


def test_subquery_echo_is_truncated() -> None:
    assert MockLM().completion("x" * 200) == "Mock: " + "x" * 50


def test_scripted_list_is_copied() -> None:
    script = ["r1"]
    lm = MockLM(script)
    script[0] = "changed"
    assert lm.completion(ROOT) == "r1"


def test_mock_passes_options_to_base() -> None:
    lm = MockLM(model_name="m2", timeout=5, sampling_args={"t": 1}, max_concurrent=3)
    assert (lm.model_name, lm.timeout, lm.sampling_args, lm.max_concurrent) == ("m2", 5, {"t": 1}, 3)


def test_cursor_is_thread_safe() -> None:
    # Checks the result is right under concurrency. Note: this test can't prove
    # the lock is needed, because the race window is too small to hit reliably.
    lm = MockLM([f"r{i}" for i in range(50)], max_concurrent=50)
    results = run_in_threads(50, lambda: lm.completion(ROOT))
    assert sorted(results) == sorted(f"r{i}" for i in range(50))  # each reply exactly once
    assert lm._cursor == 50
