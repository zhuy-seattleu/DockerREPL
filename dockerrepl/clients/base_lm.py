"""BaseLM: the interface every LM client implements, with a concurrency cap."""

import threading
from abc import ABC, abstractmethod
from typing import Any

from dockerrepl.constants import DEFAULT_TIMEOUT

# One sub-query from code in the container: plain text, or one structured message.
Prompt = str | dict[str, Any]
# The Client's root conversation: chat messages such as {"role": "user", "content": "..."}.
Messages = list[dict[str, Any]]


class BaseLM(ABC):
    """An LM client. Subclasses implement `_completion`; callers use `completion`.

    `completion` holds one of `max_concurrent` slots while the backend call runs,
    so no more than `max_concurrent` requests are ever in flight for this object.
    """

    def __init__(
        self,
        model_name: str,
        timeout: float = DEFAULT_TIMEOUT,
        sampling_args: dict[str, Any] | None = None,
        max_concurrent: int = 8,
        **kwargs: Any,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent must be at least 1, got {max_concurrent}")
        self.model_name: str = model_name
        self.timeout: float = timeout
        self.sampling_args: dict[str, Any] = sampling_args or {}
        self.max_concurrent: int = max_concurrent
        self.kwargs: dict[str, Any] = kwargs
        self._slots: threading.BoundedSemaphore = threading.BoundedSemaphore(max_concurrent)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Refuse subclasses that override `completion`, which would bypass the cap."""
        super().__init_subclass__(**kwargs)
        if "completion" in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} must not override completion(); implement _completion() instead"
            )

    def completion(self, prompt: Prompt | Messages) -> str:
        """Return the LM's reply, waiting for a free slot first."""
        with self._slots:
            return self._completion(prompt)

    @abstractmethod
    def _completion(self, prompt: Prompt | Messages) -> str:
        """Make the actual backend call. Called only by `completion`."""
