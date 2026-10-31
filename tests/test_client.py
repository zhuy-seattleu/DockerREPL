"""Step 9: Client, the round loop. Tested without Docker, using a fake environment."""

import socket
import threading
import time
from typing import Any

import pytest

import dockerrepl
from dockerrepl.client import (
    FINAL_ANSWER_REQUEST,
    MAX_RESULT_CHARS,
    NO_CODE_REMINDER,
    Client,
    build_system_prompt,
    format_result,
)
from dockerrepl.clients import MockLM
from dockerrepl.environments.container_manager import REPLResult

FINISH = '```repl\nanswer["content"] = "done"\nanswer["ready"] = True\n```'


def block(code: str) -> str:
    return f"Let me run this.\n```repl\n{code}\n```"


class RecordingLM(MockLM):
    """MockLM that keeps a copy of every root conversation it is sent."""

    def __init__(self, scripted: list[str] | None = None) -> None:
        super().__init__(scripted)
        self.conversations: list[list[dict[str, Any]]] = []

    def _completion(self, prompt: Any) -> str:
        if isinstance(prompt, list):
            self.conversations.append([dict(m) for m in prompt])
        return super()._completion(prompt)


class FakeEnv:
    """Has ContainerManager's methods. Records what it's asked to do."""

    def __init__(self, results: list[REPLResult | Exception] | None = None, delay: float = 0.0) -> None:
        self.codes: list[str] = []
        self.addresses: list[tuple[str, int]] = []
        self.cleanups = 0
        self.results = list(results or [])
        self.delay = delay
        self.running = 0  # code blocks running right now
        self.max_running = 0

    def execute_code(self, code: str) -> REPLResult:
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            time.sleep(self.delay)
            self.codes.append(code)
            if self.results:
                result = self.results.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result
            final = "done" if 'answer["ready"] = True' in code else None
            return REPLResult(stdout=f"ran: {code}\n", stderr="", final_answer=final)
        finally:
            self.running -= 1

    def update_handler_address(self, address: tuple[str, int]) -> None:
        self.addresses.append(address)

    def cleanup(self) -> None:
        self.cleanups += 1


class Envs(list):
    """Every FakeEnv the Client creates, in order. `options` configures the next ones."""

    options: dict[str, Any]


@pytest.fixture
def envs(monkeypatch: pytest.MonkeyPatch) -> Envs:
    created = Envs()
    created.options = {}

    def new_env(self: Client) -> FakeEnv:
        env = FakeEnv(**created.options)
        created.append(env)
        return env

    monkeypatch.setattr(Client, "_new_env", new_env)
    return created


def contents(conversation: list[dict[str, Any]]) -> list[str]:
    return [m["content"] for m in conversation]


# ================================================================ the loop


def test_returns_final_answer(envs: Envs) -> None:
    lm = RecordingLM([FINISH])
    assert Client(lm).completion("hi") == "done"
    assert len(lm.conversations) == 1


def test_first_conversation_is_system_then_user(envs: Envs) -> None:
    lm = RecordingLM([FINISH])
    Client(lm).completion("What is 2 + 2?")
    (conversation,) = lm.conversations
    assert [m["role"] for m in conversation] == ["system", "user"]
    assert conversation[1]["content"] == "What is 2 + 2?"


def test_multiple_iterations(envs: Envs) -> None:
    lm = RecordingLM([block("x = 1"), block("print(x)"), FINISH])
    assert Client(lm).completion("hi") == "done"
    assert len(lm.conversations) == 3
    last = lm.conversations[-1]
    assert [m["role"] for m in last] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert "ran: x = 1" in last[3]["content"] and "ran: print(x)" in last[5]["content"]


def test_blocks_run_in_order(envs: Envs) -> None:
    reply = "```repl\na = 1\n```\nthen\n```repl\nb = 2\n```"
    Client(RecordingLM([reply, FINISH])).completion("hi")
    assert envs[0].codes[:2] == ["a = 1", "b = 2"]


def test_one_feedback_message_per_reply(envs: Envs) -> None:
    # Two blocks give one user message, so roles keep alternating.
    lm = RecordingLM(["```repl\na = 1\n```\n```repl\nb = 2\n```", FINISH])
    Client(lm).completion("hi")
    feedback = lm.conversations[1][-1]
    assert feedback["role"] == "user"
    assert "ran: a = 1" in feedback["content"] and "ran: b = 2" in feedback["content"]
    assert feedback["content"].index("a = 1") < feedback["content"].index("b = 2")


def test_stops_at_first_final_answer(envs: Envs) -> None:
    reply = FINISH + "\n```repl\nprint('after')\n```"
    assert Client(RecordingLM([reply])).completion("hi") == "done"
    assert envs[0].codes == ['answer["content"] = "done"\nanswer["ready"] = True']


def test_no_code_block_gets_reminder(envs: Envs) -> None:
    lm = RecordingLM(["I think the answer is 4.", FINISH])
    Client(lm).completion("hi")
    assert lm.conversations[1][-1] == {"role": "user", "content": NO_CODE_REMINDER}


def test_errors_reach_the_lm(envs: Envs) -> None:
    envs.options = dict(results=[REPLResult(stdout="", stderr="NameError: name 'y' is not defined\n")])
    lm = RecordingLM([block("print(y)"), FINISH])
    Client(lm).completion("hi")
    assert "NameError" in lm.conversations[1][-1]["content"]


def test_max_iterations_fallback(envs: Envs) -> None:
    lm = RecordingLM([block("x = 1"), block("x = 2"), "The answer is 42."])
    assert Client(lm, max_iterations=2).completion("hi") == "The answer is 42."
    assert len(lm.conversations) == 3
    assert lm.conversations[-1][-1] == FINAL_ANSWER_REQUEST


def test_max_iterations_fallback_runs_answer_block(envs: Envs) -> None:
    # Asked for a final answer, the LM may set `answer` in a block; that is the answer.
    lm = RecordingLM([block("x = 1"), FINISH])
    assert Client(lm, max_iterations=1).completion("hi") == "done"
    assert lm.conversations[-1][-1] == FINAL_ANSWER_REQUEST


def test_lm_calls_go_through_the_handler(envs: Envs, monkeypatch: pytest.MonkeyPatch) -> None:
    from dockerrepl import client as client_module

    calls = []
    real = client_module.LMHandler.completion
    monkeypatch.setattr(client_module.LMHandler, "completion", lambda self, m: calls.append(1) or real(self, m))
    Client(RecordingLM([block("x = 1"), FINISH])).completion("hi")
    assert len(calls) == 2


# ================================================================ resource lifetimes


def test_non_persistent_new_env_each_round(envs: Envs) -> None:
    client = Client(RecordingLM([FINISH, FINISH]))
    client.completion("a")
    client.completion("b")
    assert len(envs) == 2 and [e.cleanups for e in envs] == [1, 1]


def test_cleanup_happens_after_the_round_ends(envs: Envs) -> None:
    # The handler is up while code runs; the env is cleaned up only at the end.
    Client(RecordingLM([block("x = 1"), FINISH])).completion("hi")
    assert envs[0].cleanups == 1 and len(envs[0].codes) == 2


def test_persistent_env_created_on_enter(envs: Envs) -> None:
    with Client(RecordingLM(), persistent=True):
        assert len(envs) == 1 and envs[0].addresses == []  # no handler before the first round


def test_persistent_reuses_env(envs: Envs) -> None:
    with Client(RecordingLM([FINISH, FINISH]), persistent=True) as client:
        client.completion("a")
        client.completion("b")
    assert len(envs) == 1
    first, second = envs[0].addresses
    assert first != second  # a new LMHandler each round


def test_persistent_cleanup_on_close(envs: Envs) -> None:
    with Client(RecordingLM([FINISH, FINISH]), persistent=True) as client:
        client.completion("a")
        client.completion("b")
        assert envs[0].cleanups == 0
    assert envs[0].cleanups == 1


def test_close_is_idempotent(envs: Envs) -> None:
    client = Client(RecordingLM(), persistent=True)
    client.__enter__()
    client.close()
    client.close()
    assert envs[0].cleanups == 1


def test_persistent_without_with(envs: Envs) -> None:
    client = Client(RecordingLM([FINISH, FINISH]), persistent=True)
    client.completion("a")
    client.completion("b")
    assert len(envs) == 1 and envs[0].cleanups == 0
    client.close()
    assert envs[0].cleanups == 1


def test_handler_stopped_after_round(envs: Envs) -> None:
    Client(RecordingLM([FINISH])).completion("hi")
    (address,) = envs[0].addresses
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(address, timeout=2).close()


def test_cleanup_on_exception(envs: Envs) -> None:
    envs.options = dict(results=[RuntimeError("the container is not running")])
    with pytest.raises(RuntimeError, match="not running"):
        Client(RecordingLM([block("x = 1")])).completion("hi")
    assert envs[0].cleanups == 1


def test_failed_container_start_leaves_no_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    # Container first, then handler: if the container can't start, no handler was ever started.
    from dockerrepl import client as client_module

    started, stopped = [], []
    real_start, real_stop = client_module.LMHandler.start, client_module.LMHandler.stop
    monkeypatch.setattr(client_module.LMHandler, "start", lambda self: started.append(1) or real_start(self))
    monkeypatch.setattr(client_module.LMHandler, "stop", lambda self: stopped.append(1) or real_stop(self))

    def fail(self: Client) -> FakeEnv:
        raise RuntimeError("docker run failed")

    monkeypatch.setattr(Client, "_new_env", fail)
    with pytest.raises(RuntimeError, match="docker run failed"):
        Client(RecordingLM([FINISH])).completion("hi")
    assert len(started) == len(stopped)


def test_persistent_rounds_take_turns(envs: Envs) -> None:
    # Two threads share one persistent container; their code must never overlap.
    envs.options = dict(delay=0.05)
    lm = RecordingLM([block("a = 1"), FINISH, block("b = 1"), FINISH])
    with Client(lm, persistent=True) as client:
        threads = [threading.Thread(target=client.completion, args=(p,)) for p in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
    assert len(envs[0].codes) == 4 and envs[0].max_running == 1


def test_non_persistent_rounds_run_in_parallel(envs: Envs) -> None:
    # Each round has its own container, so there's nothing to wait for.
    envs.options = dict(delay=0.3)
    client = Client(RecordingLM())  # every reply is MOCK_FINAL_REPLY: one block per round
    threads = [threading.Thread(target=client.completion, args=("x",)) for _ in range(4)]
    start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert time.perf_counter() - start < 4 * 0.3 / 2
    assert len(envs) == 4


# ================================================================ construction


def test_invalid_tools_rejected_early(envs: Envs) -> None:
    with pytest.raises(ValueError, match="reserved"):
        Client(MockLM(), custom_tools={"answer": 1})
    assert envs == []


@pytest.mark.parametrize("n", [0, -1])
def test_max_iterations_must_be_positive(n: int) -> None:
    with pytest.raises(ValueError, match="max_iterations"):
        Client(MockLM(), max_iterations=n)


def test_package_exports() -> None:
    assert dockerrepl.Client is Client and dockerrepl.MockLM is MockLM
    assert issubclass(dockerrepl.MockLM, dockerrepl.BaseLM)


# ================================================================ prompts


def test_system_prompt_mentions_helpers_and_tools() -> None:
    (message,) = build_system_prompt({"triple": {"tool": "def triple(x):\n    return 3 * x", "description": "triples"}})
    assert message["role"] == "system"
    for word in ("```repl", "llm_query", "llm_query_batched", "SHOW_VARS", 'answer["ready"] = True', "persist"):
        assert word in message["content"]
    assert "- triple: triples" in message["content"]


def test_system_prompt_without_tools() -> None:
    (message,) = build_system_prompt(None)
    assert "custom tools" not in message["content"]


def test_system_prompt_example_blocks_parse() -> None:
    # The examples in the prompt must themselves be valid repl blocks.
    from dockerrepl.parsing import find_code_blocks

    (message,) = build_system_prompt(None)
    assert find_code_blocks(message["content"]) == [
        "x = 21 * 2\nprint(x)",
        'answer["content"] = "your final answer"\nanswer["ready"] = True',
    ]


def test_format_result_shows_code_output_and_errors() -> None:
    text = format_result("print(1)", REPLResult(stdout="1\n", stderr="warn\n"))
    assert "```repl\nprint(1)\n```" in text
    assert text.index("1\n") < text.index("warn")


def test_format_result_without_output() -> None:
    text = format_result("x = 1", REPLResult(stdout="", stderr=""))
    assert "(no output)" in text and "Errors" not in text


def test_format_result_truncates() -> None:
    text = format_result("x", REPLResult(stdout="a" * 20_000, stderr="b" * 20_000))
    assert text.count("[truncated") == 2
    assert MAX_RESULT_CHARS * 2 < len(text) < MAX_RESULT_CHARS * 2 + 300
