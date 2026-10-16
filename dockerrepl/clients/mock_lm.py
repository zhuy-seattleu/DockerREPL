"""MockLM: a deterministic, zero-cost LM for development and tests."""

import threading
from typing import Any

from dockerrepl.clients.base_lm import BaseLM, Messages, Prompt

# Returned once the script runs out, so a round always ends.
MOCK_FINAL_REPLY: str = (
    "Done.\n"
    "```repl\n"
    'answer["content"] = "mock final answer"\n'
    'answer["ready"] = True\n'
    "```"
)


class MockLM(BaseLM):
    """Replies from a script to the root conversation, and echoes sub-queries.

    A `list` prompt is the Client's root conversation: it gets the next scripted
    reply. A `str` or `dict` prompt is a sub-query from code in the container:
    it gets `"Mock: <prompt>"` and does not use up a scripted reply.
    """

    def __init__(self, scripted: list[str] | None = None, model_name: str = "mock", **kwargs: Any) -> None:
        super().__init__(model_name, **kwargs)
        self.scripted: list[str] = list(scripted or [])
        self._cursor: int = 0  # index in `scripted` of the next reply
        self._lock: threading.Lock = threading.Lock()  # guards _cursor

    def _completion(self, prompt: Prompt | Messages) -> str:
        if isinstance(prompt, list):
            with self._lock:
                if self._cursor < len(self.scripted):
                    reply = self.scripted[self._cursor]
                    self._cursor += 1
                    return reply
            return MOCK_FINAL_REPLY
        return f"Mock: {str(prompt)[:50]}"
