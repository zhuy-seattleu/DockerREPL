# Step 2 — `BaseLM` and `MockLM`

## Goal

The LM abstraction that everything else talks to:

* **`BaseLM`** is an abstract base class. Its `completion()` method caps how
  many backend calls can run at once, and subclasses can't get around it.
* **`MockLM`** is a deterministic, zero-cost implementation. It replies from a
  script to the Client's root conversation, and echoes sub-queries from the
  container.

## Tasks at a glance

1. **Add shared settings.** Create `dockerrepl/constants.py` with
   `DEFAULT_TIMEOUT = 300` (seconds), which later steps reuse.
2. **Name the two kinds of LM input.** In `dockerrepl/clients/base_lm.py`,
   define `Prompt` (one sub-query: a `str` or one `dict` message) and
   `Messages` (the Client's root conversation: a `list` of message dicts).
3. **Write the abstract `BaseLM`.** It stores `model_name`, `timeout`,
   `sampling_args`, `max_concurrent` and extra `**kwargs`. Its public
   `completion()` holds one slot of a `BoundedSemaphore` while the abstract
   `_completion()` runs. It also refuses any subclass that overrides
   `completion()`.
4. **Write `MockLM`.** A root conversation gets the next scripted reply, and a
   final-answer reply once the script runs out. A sub-query gets
   `"Mock: <prompt>"`. A lock guards the script position, `_cursor`.
5. **Export the public names** from `dockerrepl/clients/__init__.py`.
6. **Write the unit tests.** `tests/test_lm_clients.py` has 18 tests,
   including concurrency tests that start all threads together with a
   `threading.Barrier`.
7. **Check that the tests catch real bugs** by making each mistake in the
   Pitfalls table on purpose.

**Outcome:** `pytest -q` shows 18 more passing tests, and the CI table shows
step 2 as ✅.

## Two kinds of LM input

| | **Root conversation** (`Messages`) | **Sub-query** (`Prompt`) |
|---|---|---|
| What it is | the conversation that drives a round: system prompt, the user's question, the LM's earlier replies, code results | a question that LM-written code asks while it runs in the container |
| Example | `[{"role": "system", ...}, {"role": "user", "content": "Sum 1..100?"}]` | `llm_query("Summarize this chunk")` |
| Sent by | the Client, directly via `LMHandler.completion()` (step 9) | the container, via the proxy and LMHandler (steps 6–7) |
| Shape | `list[dict[str, Any]]` | `str` or `dict[str, Any]` |

Both are **one** request with **one** reply. A `list` is one conversation, not
several prompts. **Several independent prompts** are a batch, which has its
own path: `llm_query_batched([...])` → `LMRequest.prompts` → one
`completion()` call per prompt (steps 3–4). That's why `Prompt` doesn't
include `list[str]`: a list given to `completion()` always means a
conversation.

A real LM treats both kinds the same way. Only MockLM tells them apart, and it
does so by shape alone, which is safe because nothing from the container can
be a list (`LMRequest.prompt` is a `Prompt`).

## What you'll learn

* **Abstract base classes** (`abc.ABC`, `@abstractmethod`), and
  **`__init_subclass__`** to enforce a rule on every subclass.
* **The template-method pattern:** a fixed public method, `completion()`,
  wraps a hook that subclasses implement, `_completion()`.
* **`threading.BoundedSemaphore`** to limit concurrency, and
  **`threading.Lock`** to make a read-then-update atomic.
* **Testing concurrency:** start threads together with `threading.Barrier`,
  measure the peak number of calls in flight, and use daemon threads so a
  stuck thread fails a test instead of hanging the run.
* **What tests can't prove:** some race conditions are real but too rare to
  reproduce. See the Pitfalls section.

## Depends on

Step 0. The tests also use `find_code_blocks` from step 1.

## Files

| File | Purpose |
|---|---|
| `dockerrepl/constants.py` | `DEFAULT_TIMEOUT` |
| `dockerrepl/clients/base_lm.py` | `Prompt`, `Messages`, `BaseLM` |
| `dockerrepl/clients/mock_lm.py` | `MOCK_FINAL_REPLY`, `MockLM` |
| `dockerrepl/clients/__init__.py` | re-exports the five names above |
| `tests/test_lm_clients.py` | the tests below |

## Interface

```python
# constants.py
DEFAULT_TIMEOUT: int = 300          # seconds

# clients/base_lm.py
Prompt = str | dict[str, Any]              # one sub-query
Messages = list[dict[str, Any]]            # the root conversation

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

    def __init_subclass__(cls, **kwargs: Any) -> None: ...   # refuses overrides of completion()
    def completion(self, prompt: Prompt | Messages) -> str: ...
    @abstractmethod
    def _completion(self, prompt: Prompt | Messages) -> str: ...

# clients/mock_lm.py
MOCK_FINAL_REPLY: str   # a reply whose repl block sets answer["content"] and answer["ready"] = True

class MockLM(BaseLM):
    def __init__(self, scripted: list[str] | None = None,
                 model_name: str = "mock", **kwargs: Any) -> None: ...
    scripted: list[str]
    _cursor: int            # index in `scripted` of the next reply
    _lock: threading.Lock   # guards _cursor
    def _completion(self, prompt: Prompt | Messages) -> str: ...
```

## Implementation guide

1. **`BaseLM.__init__`:**
   - Raise `ValueError` if `max_concurrent < 1`.
   - Store every argument. Store `sampling_args or {}`, so later code never
     has to check for `None`.
   - Create `self._slots = threading.BoundedSemaphore(max_concurrent)`.
2. **`BaseLM.completion`** is `with self._slots: return self._completion(prompt)`.
   The `with` block releases the slot even if `_completion` raises.
3. **`BaseLM.__init_subclass__`** runs once for every class defined as a
   subclass. If `"completion" in cls.__dict__`, raise `TypeError` telling the
   author to implement `_completion()` instead.
4. **`MOCK_FINAL_REPLY`:**

   ````text
   Done.
   ```repl
   answer["content"] = "mock final answer"
   answer["ready"] = True
   ```
   ````

5. **`MockLM.__init__`:** pass `model_name` and `**kwargs` on to
   `BaseLM.__init__`. Copy the script with `list(scripted or [])`, so the
   caller can't change it later. Start `_cursor` at 0 and create `_lock`.
6. **`MockLM._completion`:**
   - **A `list` (root conversation):** under `_lock`, if
     `_cursor < len(scripted)`, return `scripted[_cursor]` and add one to
     `_cursor`. Otherwise return `MOCK_FINAL_REPLY`.
   - **Anything else (sub-query):** return `f"Mock: {str(prompt)[:50]}"` and
     leave `_cursor` alone.

## Tests — `tests/test_lm_clients.py`

Helpers at the top of the test file:

* `EchoLM`: the smallest subclass; its `_completion` returns `str(prompt)`.
* `SlowLM`: counts the calls inside `_completion` (under its own lock),
  records the peak, sleeps 0.05 s, and returns `"ok"`.
* `FailingLM`: its `_completion` always raises `RuntimeError`.
* `run_in_threads(n, target, timeout=10)`: starts `n` **daemon** threads that
  wait at a `threading.Barrier`, then run `target()` together. It fails the
  test if any thread is still running after `timeout`.
* `ROOT`: a one-message root conversation.

| Test | Checks |
|---|---|
| `test_base_lm_is_abstract` | `BaseLM("x")` raises `TypeError` |
| `test_base_lm_stores_options` | `EchoLM` keeps `model_name`, `timeout`, `sampling_args` and `kwargs` (for example `api_key="k"`) |
| `test_defaults` | `timeout == DEFAULT_TIMEOUT`, `sampling_args == {}`, `max_concurrent == 8` |
| `test_max_concurrent_must_be_positive` | `max_concurrent=0` raises `ValueError` |
| `test_overriding_completion_is_refused` | defining a subclass that overrides `completion()` raises `TypeError` mentioning `_completion` |
| `test_semaphore_caps_concurrency` | `SlowLM(max_concurrent=2)` with 10 threads: all succeed, and the peak is **exactly** 2 |
| `test_calls_run_in_parallel_up_to_the_cap` | `SlowLM(max_concurrent=5)` with 5 threads: the peak is 5, so calls really overlap |
| `test_slot_released_after_exception` | `FailingLM(max_concurrent=1)`: two calls in a row both raise, and the second doesn't block (2 s timeout) |
| `test_mock_returns_scripted_in_order` | `MockLM(["r1", "r2"])` with root conversations gives `"r1"`, then `"r2"` |
| `test_mock_falls_back_when_script_exhausted` | after the script, every root call returns `MOCK_FINAL_REPLY` |
| `test_empty_script_goes_straight_to_final_reply` | `MockLM()` with a root conversation returns `MOCK_FINAL_REPLY` |
| `test_final_reply_contains_ready_block` | `find_code_blocks(MOCK_FINAL_REPLY)` gives one block containing `answer["ready"] = True`, and it compiles |
| `test_subquery_echo_does_not_advance_cursor` | `completion("hello")` gives `"Mock: hello"`; the next root call still gives `"r1"` |
| `test_dict_prompt_is_subquery` | a `dict` prompt gets an echo and doesn't move `_cursor` |
| `test_subquery_echo_is_truncated` | a 200-character prompt is echoed as `"Mock: "` plus 50 characters |
| `test_scripted_list_is_copied` | changing the caller's list after construction doesn't change the replies |
| `test_mock_passes_options_to_base` | `MockLM(model_name=..., timeout=..., sampling_args=..., max_concurrent=...)` stores all four |
| `test_cursor_is_thread_safe` | 50 threads, each with one root call, get each of 50 scripted replies exactly once, and `_cursor == 50` |

## Run

```bash
pytest -v tests/test_lm_clients.py
```

## Done when

- [ ] All 18 tests pass, and steps 0–1 still pass.
- [ ] Running `tests/test_lm_clients.py` five times in a row passes every
      time, so the concurrency tests aren't flaky.
- [ ] Each mistake in the Pitfalls table, made on purpose, gives the result
      listed there. Undo it afterwards.
- [ ] The **Tests** workflow on GitHub shows step 2 as ✅.

## Pitfalls

Each row was checked by making the mistake on purpose:

| Mistake | What goes wrong | Caught by |
|---|---|---|
| **No semaphore** in `completion()` | unlimited concurrent backend calls | `test_semaphore_caps_concurrency` (peak 10, not 2) |
| **`acquire()`/`release()` without `try/finally`** | one exception leaks a slot forever; with `max_concurrent=1` every later call hangs | `test_slot_released_after_exception` fails after 2 s. Without daemon threads the whole test run would hang instead. Use `with self._slots:`. |
| **Detecting root conversations with `not isinstance(prompt, str)`** | a `dict` sub-query uses up a scripted reply, and the script gets out of step with the round | `test_dict_prompt_is_subquery` |
| **Not copying `scripted`** | the caller can change MockLM's replies afterwards | `test_scripted_list_is_copied` |
| **Keeping `sampling_args=None`** | later code needs `if sampling_args:` checks everywhere | `test_defaults` |
| **No lock around `_cursor`** | two threads can read the same `_cursor` and get the same reply | **not caught by any test**, see below |

**About the lock.** Without `_lock`, two threads could both read `_cursor`
before either adds one, so a scripted reply is returned twice and another is
skipped. The window for that is a few bytecode instructions long, so in
practice the test passes anyway, even across many runs. It passed 10 out of
10 times with the lock removed, even with Python told to switch threads very
often. A test can show that code *can* fail, but it can't prove that code
*can't*. So the lock stays because the reasoning says it's needed, not because
a test demands it. That's a general lesson about concurrent code.

Also avoid `threading.Semaphore` in place of `BoundedSemaphore`: a stray
extra `release()` would silently raise the cap, where `BoundedSemaphore` raises
`ValueError` instead.
