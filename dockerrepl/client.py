"""Client: the class users talk to. One `completion()` call is one round.

Each round: pick the container (the session's, or a new one), start a new
LMHandler and point the container's proxy at it, then iterate: ask the LM,
run the ```repl blocks in its reply, and feed the results back, until a block
sets `answer["ready"] = True` or `max_iterations` replies have been used.
"""

import contextlib
import threading
from typing import Any

from dockerrepl.clients.base_lm import BaseLM, Messages
from dockerrepl.environments.container_manager import ContainerManager, REPLResult
from dockerrepl.environments.tools import format_tools_for_prompt, validate_custom_tools
from dockerrepl.lm_handler import LMHandler
from dockerrepl.parsing import find_code_blocks

# Longest stdout or stderr, per code block, sent back to the LM.
MAX_RESULT_CHARS: int = 5_000

SYSTEM_PROMPT = """\
You are working in a Python REPL that runs in a sandboxed container.

To run code, put it in a ```repl block:
```repl
x = 21 * 2
print(x)
```
After each of your replies, every ```repl block in it runs in order, and you
are shown what it printed and any errors.

Variables, functions and imports persist between blocks and between replies.

These are already defined:
- llm_query(prompt) -> str: ask a language model one question; returns its reply.
- llm_query_batched(prompts) -> list[str]: ask several questions at once, in parallel.
- SHOW_VARS() -> str: list the variables you have created.
Errors from llm_query come back as strings starting with "Error:".

When you have the final answer, finish in a ```repl block:
```repl
answer["content"] = "your final answer"
answer["ready"] = True
```
The round ends as soon as answer["ready"] is True; later blocks don't run.
"""

NO_CODE_REMINDER: str = (
    "Your reply had no ```repl block, so nothing ran. Write code in a ```repl block, "
    'or finish by setting answer["content"] and then answer["ready"] = True in one.'
)

FINAL_ANSWER_REQUEST: dict[str, Any] = {
    "role": "user",
    "content": (
        "You have used all your iterations. Give your final answer now: set "
        'answer["content"] and answer["ready"] = True in a ```repl block, '
        "or reply with the answer as plain text."
    ),
}


def _message(role: str, content: str) -> dict[str, Any]:
    return {"role": role, "content": content}


def build_system_prompt(custom_tools: dict[str, Any] | None) -> Messages:
    """The system message: REPL conventions, helpers, how to finish, and any custom tools."""
    text = SYSTEM_PROMPT
    tools = format_tools_for_prompt(custom_tools)
    if tools:
        text += "\nAlso defined, as custom tools:\n" + tools + "\n"
    return [_message("system", text)]


def _cut(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + f"\n... [truncated, {len(text) - MAX_RESULT_CHARS} more characters]"


def format_result(code: str, result: REPLResult) -> str:
    """One code block's run, as text for the LM: the code, its output, and its errors."""
    parts = [f"Code:\n```repl\n{code}\n```", "Output:\n" + (_cut(result.stdout) or "(no output)")]
    if result.stderr:
        parts.append("Errors:\n" + _cut(result.stderr))
    return "\n".join(parts)


class Client:
    """Runs rounds against an LM, with code executed in a Docker container.

    With `persistent=True`, one container serves every round, so variables carry
    over; use the Client as a context manager, or call close(). Otherwise each
    round gets a fresh container that is removed when the round ends.
    """

    def __init__(
        self,
        lm: BaseLM,
        persistent: bool = False,
        custom_tools: dict[str, Any] | None = None,
        max_iterations: int = 10,
        image: str = "python:3.11-slim",
    ) -> None:
        validate_custom_tools(custom_tools)  # fail now, not when the first container starts
        if max_iterations < 1:
            raise ValueError(f"max_iterations must be at least 1, not {max_iterations}")
        self.lm: BaseLM = lm
        self.persistent: bool = persistent
        self.custom_tools: dict[str, Any] | None = custom_tools
        self.max_iterations: int = max_iterations
        self.image: str = image
        self._env: ContainerManager | None = None  # the session's container (persistent)
        # A persistent container runs one code block at a time, so its rounds take turns.
        self._session_lock: threading.Lock = threading.Lock()

    def _new_env(self) -> ContainerManager:
        return ContainerManager(self.image, persistent=self.persistent, custom_tools=self.custom_tools)

    def completion(self, prompt: str) -> str:
        """Run one round for `prompt` and return the final answer."""
        lock = self._session_lock if self.persistent else contextlib.nullcontext()
        with lock:
            # 1. Container: the session's one, or a fresh one for this round.
            if self.persistent:
                if self._env is None:  # used without `with`
                    self._env = self._new_env()
                env = self._env
            else:
                env = self._new_env()
            try:
                # 2. A new LMHandler for this round; point the proxy at it.
                with LMHandler(self.lm) as lm_handler:
                    env.update_handler_address(lm_handler.address)
                    return self._iterate(env, lm_handler, prompt)
            finally:
                if not self.persistent:
                    env.cleanup()

    def _iterate(self, env: ContainerManager, lm_handler: LMHandler, prompt: str) -> str:
        # 3. Iterations: one LM reply each.
        messages = build_system_prompt(self.custom_tools) + [_message("user", prompt)]
        for _ in range(self.max_iterations):
            reply = lm_handler.completion(messages)
            messages.append(_message("assistant", reply))
            answer, feedback = self._run_blocks(env, reply)
            if answer is not None:
                return answer
            messages.append(_message("user", feedback or NO_CODE_REMINDER))
        # 4. Out of iterations: ask once more. The LM may answer in a block or in plain text.
        reply = lm_handler.completion(messages + [FINAL_ANSWER_REQUEST])
        answer, _ = self._run_blocks(env, reply)
        return answer if answer is not None else reply

    @staticmethod
    def _run_blocks(env: ContainerManager, reply: str) -> tuple[str | None, str]:
        """Run the reply's blocks in order. Return (final answer or None, feedback text)."""
        results = []
        for code in find_code_blocks(reply):
            result = env.execute_code(code)
            if result.final_answer is not None:
                return result.final_answer, ""  # later blocks don't run
            results.append(format_result(code, result))
        return None, "\n\n".join(results)  # one message for the whole reply

    def close(self) -> None:
        """Remove the session's container, if there is one. Safe to call twice."""
        if self._env is not None:
            self._env.cleanup()
            self._env = None

    def __enter__(self) -> "Client":
        if self.persistent:
            self._env = self._new_env()  # set up once for the session
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
