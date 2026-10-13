# Step 2 — `BaseLM` and `MockLM`

## Goal

The LM abstraction everything else talks to:

* **`BaseLM`** is an abstract base class. Its `completion()` method caps how
  many backend calls can run at once.
* **`MockLM`** is a deterministic, zero-cost implementation. It returns
  scripted replies to the Client and echo replies to code in the container.

## What you'll learn

* **Abstract base classes** (`abc.ABC`, `@abstractmethod`).
* **The template-method pattern:** a fixed public method, `completion()`,
  wraps a hook that subclasses implement, `_completion()`.
* **`threading.BoundedSemaphore`** to limit concurrency, and
  **`threading.Lock`** to make a read-then-update atomic.
* **Testing concurrency without sleeping blindly:** measure the peak number
  of calls in flight.

## Depends on

Step 0. The tests also use `find_code_blocks` from step 1.

## Files

| File | Purpose |
|---|---|
| `dockerrepl/constants.py` | `DEFAULT_TIMEOUT` |
| `dockerrepl/clients/base_lm.py` | `Prompt`, `BaseLM` |
| `dockerrepl/clients/mock_lm.py` | `MOCK_FINAL_REPLY`, `MockLM` |
| `dockerrepl/clients/__init__.py` | re-export `BaseLM`, `MockLM` |
| `tests/test_lm_clients.py` | tests below |

## Interface

```python
# constants.py
DEFAULT_TIMEOUT: int = 300          # seconds

# clients/base_lm.py
Prompt = str | dict[str, Any] | list[dict[str, Any]]

class BaseLM(ABC):
    def __init__(self, model_name: str, timeout: float = DEFAULT_TIMEOUT,
                 sampling_args: dict[str, Any] | None = None,
                 max_concurrent: int = 8, **kwargs: Any) -> None: ...
    model_name: str
    timeout: float
    sampling_args: dict[str, Any]          # {} when None was passed
    max_concurrent: int
    kwargs: dict[str, Any]
    _slots: threading.BoundedSemaphore

    def completion(self, prompt: Prompt) -> str: ...      # do not override
    @abstractmethod
    def _completion(self, prompt: Prompt) -> str: ...

# clients/mock_lm.py
MOCK_FINAL_REPLY: str   # a reply whose repl block sets answer["content"] and answer["ready"] = True

class MockLM(BaseLM):
    def __init__(self, scripted: list[str] | None = None,
                 model_name: str = "mock", **kwargs: Any) -> None: ...
    scripted: list[str]
    _cursor: int
    _lock: threading.Lock
    def _completion(self, prompt: Prompt) -> str: ...
```

## Implementation guide

1. **`BaseLM.__init__`:**
   - Store every argument.
   - Store `sampling_args or {}`, so later code never has to check for `None`.
   - Raise `ValueError` if `max_concurrent < 1`.
   - Create `self._slots = threading.BoundedSemaphore(max_concurrent)`.
2. **`BaseLM.completion`** is `with self._slots: return self._completion(prompt)`.
   The `with` block releases the slot even if `_completion` raises.
3. **`MOCK_FINAL_REPLY`:**

   ````text
   Done.
   ```repl
   answer["content"] = "mock final answer"
   answer["ready"] = True
   ```
   ````

4. **`MockLM._completion`:**
   - **If `prompt` is a `list`** (a root prompt from the Client): take
     `_lock`. If `_cursor < len(scripted)`, return `scripted[_cursor]` and add
     one to `_cursor`. Otherwise return `MOCK_FINAL_REPLY`.
   - **Otherwise** (a `str` or `dict` from `llm_query`): return
     `f"Mock: {str(prompt)[:50]}"` and leave `_cursor` alone.
5. **`MockLM.__init__`:** copy the list (`list(scripted or [])`), so the
   caller can't change it later. Pass `model_name` and `**kwargs` on to
   `BaseLM.__init__`.

## Tests — `tests/test_lm_clients.py`

Helpers to write at the top of the test file:

* `SlowLM(BaseLM)`: its `_completion` adds one to a shared in-flight counter
  (under a lock) and records the peak value. It then sleeps 0.05 s, takes one
  off the counter, and returns `"ok"`.
* `FailingLM(BaseLM)`: its `_completion` always raises `RuntimeError`.

| Test | Checks |
|---|---|
| `test_base_lm_is_abstract` | `BaseLM("x")` raises `TypeError` |
| `test_base_lm_stores_options` | a minimal subclass keeps `model_name`, `timeout`, `sampling_args` and `kwargs` (for example `api_key="k"`) |
| `test_sampling_args_default_to_empty_dict` | `sampling_args=None` is stored as `{}` |
| `test_max_concurrent_must_be_positive` | `max_concurrent=0` raises `ValueError` |
| `test_semaphore_caps_concurrency` | `SlowLM(max_concurrent=2)`, 10 threads each calling `completion("p")`: the peak in flight is exactly 2 |
| `test_slot_released_after_exception` | `FailingLM(max_concurrent=1)`: two calls in a row both raise `RuntimeError`, and the second doesn't hang |
| `test_mock_returns_scripted_in_order` | `MockLM(["r1", "r2"])` with root prompts `[{"role": "user", ...}]` gives `"r1"`, then `"r2"` |
| `test_mock_falls_back_when_script_exhausted` | the third root call returns `MOCK_FINAL_REPLY` |
| `test_final_reply_contains_ready_block` | `find_code_blocks(MOCK_FINAL_REPLY)` has one block containing `answer["ready"] = True` |
| `test_subquery_echo_does_not_advance_cursor` | `completion("hello")` gives `"Mock: hello"`; the next root call still gives `"r1"` |
| `test_dict_prompt_is_subquery` | a `dict` prompt gives an echo and doesn't move `_cursor` |
| `test_scripted_list_is_copied` | changing the caller's list after construction doesn't change the replies |
| `test_cursor_is_thread_safe` | `MockLM([f"r{i}" for i in range(50)], max_concurrent=50)`, 50 threads each make one root call: the set of replies is exactly `{"r0", …, "r49"}` |

## Run

```bash
pytest -q tests/test_lm_clients.py
```

## Done when

- [ ] All tests pass, and steps 0–1 still pass.
- [ ] No subclass overrides `completion()`.
- [ ] `pytest -q tests/test_lm_clients.py` takes under 2 seconds.

## Pitfalls

* **Using `Semaphore` instead of `BoundedSemaphore`.** A stray extra
  `release()` would silently raise the cap. `BoundedSemaphore` raises
  `ValueError` instead.
* **Releasing the slot by hand** with `acquire()` and `release()` without
  `try/finally`. An exception would leak the slot forever. Use `with`.
* **Checking `isinstance(prompt, str)` to detect root prompts.** `dict`
  prompts would then be treated as root prompts. Check for `list`.
* **Asserting peak `<= 2` only.** That also passes if no concurrency happens
  at all. Assert `== 2`.
