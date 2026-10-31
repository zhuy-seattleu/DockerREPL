# Step 9 — `Client` (the round loop)

## Goal

The class users talk to. It owns the session's container (persistent mode)
or creates one per round (non-persistent mode). Each round it starts a new
LMHandler, points the container at it, and runs the iteration loop: ask the
LM, run its code blocks, feed the results back, and stop at a final answer
or after `max_iterations`.

It is tested **without Docker**, by replacing the container with a fake
environment. The real container is used in step 10.

## Tasks at a glance

1. **The prompts:** `SYSTEM_PROMPT` and `build_system_prompt`,
   `NO_CODE_REMINDER`, `FINAL_ANSWER_REQUEST`, and `format_result`, which
   cuts long output.
2. **`__init__`:** validate the tools and `max_iterations`; start nothing.
3. **Session scope:** `__enter__` starts a persistent container; `close()`
   and `__exit__` remove it.
4. **`completion(prompt)`:** container, then a new LMHandler, then the
   iterations, then cleanup in `finally`. Persistent rounds hold a lock.
5. **`_run_blocks`:** run one reply's blocks in order, stop at the first final
   answer, and return all the results as one message.
6. **The fallback** after `max_iterations`: ask once more, and accept an
   answer set in a block or given as plain text.
7. **Re-export** `Client`, `MockLM` and `BaseLM` from `dockerrepl`.
8. **Write the unit tests.** `tests/test_client.py` has 32 tests (33 cases
   with parametrize), all without Docker.
9. **Check that the tests catch real bugs** by making each mistake in the
   Pitfalls table on purpose.

**Outcome:** `pytest -q` shows 33 more passing test cases, and the CI table
shows step 9 as ✅.

## What you'll learn

* **Orchestration:** putting components together in the right order
  (container → handler → iterations → handler stop → container cleanup).
* **Resource lifetimes:** session scope vs round scope, and `try/finally` so
  cleanup always happens.
* **Locks for shared resources:** which rounds may run at once, and which
  must take turns.
* **Test doubles:** a `FakeEnv` with the same interface as
  `ContainerManager`, injected with `monkeypatch`.
* **Prompt construction:** a system prompt that teaches the LM the REPL
  conventions, and a conversation whose roles alternate.

## Depends on

Step 1 (`find_code_blocks`), step 2 (`MockLM`), step 4 (`LMHandler`), step 5
(`validate_custom_tools`, `format_tools_for_prompt`) and step 8
(`ContainerManager`, `REPLResult`).

## Files

| File | Purpose |
|---|---|
| `dockerrepl/client.py` | prompt helpers, `Client` |
| `dockerrepl/__init__.py` | re-export `Client`, `MockLM`, `BaseLM` |
| `tests/test_client.py` | tests below |

## Interface

```python
# Messages (step 2) = list[dict[str, Any]]; each dict is {"role": "system" | "user" | "assistant", "content": str}

MAX_RESULT_CHARS: int = 5_000            # per stdout/stderr, per block, sent to the LM
SYSTEM_PROMPT: str
NO_CODE_REMINDER: str                    # sent when a reply has no repl block
FINAL_ANSWER_REQUEST: dict[str, Any]     # user message asking for a final answer

def build_system_prompt(custom_tools: dict[str, Any] | None) -> Messages: ...
def format_result(code: str, result: REPLResult) -> str: ...

class Client:
    def __init__(self, lm: BaseLM, persistent: bool = False,
                 custom_tools: dict[str, Any] | None = None,
                 max_iterations: int = 10,
                 image: str = "python:3.11-slim") -> None: ...
    lm: BaseLM
    persistent: bool
    custom_tools: dict[str, Any] | None
    max_iterations: int
    image: str
    _env: ContainerManager | None
    def _new_env(self) -> ContainerManager: ...
    def completion(self, prompt: str) -> str: ...
    def close(self) -> None: ...
    def __enter__(self) -> "Client": ...
    def __exit__(self, *exc: object) -> None: ...
```

## Implementation guide

1. **`SYSTEM_PROMPT`** explains:
   - how to write ```` ```repl ```` blocks, with an example;
   - that variables, functions and imports persist between blocks;
   - `llm_query(prompt)`, `llm_query_batched(prompts)` and `SHOW_VARS()`,
     and that errors come back as `"Error: ..."` strings;
   - how to finish: set `answer["content"]`, then `answer["ready"] = True`,
     in a block. The round ends at once, and later blocks don't run.

   **`build_system_prompt(custom_tools)`** returns
   `[{"role": "system", "content": text}]`. It adds the lines from
   `format_tools_for_prompt` (step 5) if there are tools. The prompt's own
   examples are real ```` ```repl ```` blocks, so a test checks that
   `find_code_blocks` reads them correctly.
2. **`format_result(code, result)`:** the code in a ```` ```repl ```` block,
   then `Output:` with stdout (or `(no output)`), then `Errors:` with stderr
   if there is any. Cut stdout and stderr to `MAX_RESULT_CHARS` each, with a
   `... [truncated, N more characters]` note, so one noisy print can't fill
   the context.
3. **`__init__`:** call `validate_custom_tools` (step 5), and raise
   `ValueError` if `max_iterations < 1`. Start **nothing**: no container, no
   handler. Make a `threading.Lock` for persistent rounds.
4. **`__enter__`** starts the container (`self._env = self._new_env()`) if
   `persistent`, and returns `self`. **`__exit__`** calls `close()`.
5. **`_new_env`** returns
   `ContainerManager(self.image, persistent=self.persistent, custom_tools=self.custom_tools)`.
   Keeping it a separate method is what lets tests replace it.
6. **`completion(prompt)`** follows the sketch in `ARCHITECTURE.md` §3.6:
   1. In persistent mode, hold the Client's lock for the whole round, so two
      threads can't run code in the same container at once. Non-persistent
      rounds don't need it: each has its own container.
   2. Pick the environment: the session's (created if `None`) or a new one.
   3. `try:` then `with LMHandler(self.lm) as h:` then
      `env.update_handler_address(h.address)`.
   4. Start `messages` with the system prompt and `{"role": "user", "content": prompt}`.
      Loop up to `max_iterations` times:
      - `reply = h.completion(messages)`, and append it as `assistant`.
      - `answer, feedback = _run_blocks(env, reply)`. If `answer` is set,
        return it.
      - Otherwise append `feedback` as **one** `user` message, or
        `NO_CODE_REMINDER` if the reply had no blocks.
   5. **After the loop:** `reply = h.completion(messages + [FINAL_ANSWER_REQUEST])`.
      Run its blocks the same way. Return the answer if one was set,
      otherwise the reply text.
   6. `finally:` if not persistent, `env.cleanup()`.
7. **`_run_blocks(env, reply)`:** for each block from
   `find_code_blocks(reply)`, call `env.execute_code`. If `final_answer` is
   not `None`, return `(final_answer, "")` at once, so later blocks don't
   run. Otherwise return `(None, "\n\n".join(format_result(...) for each block))`.
   One message for the whole reply keeps the roles alternating
   (`user`, `assistant`, `user`, ...). Some LM APIs reject two `user`
   messages in a row.
8. **`close()`** cleans up `_env` if there is one, then sets it to `None`. It
   is safe to call twice.
9. **`dockerrepl/__init__.py`:** `from dockerrepl.client import Client` and
   `from dockerrepl.clients import BaseLM, MockLM`, so users can write
   `from dockerrepl import Client, MockLM`.

## Tests — `tests/test_client.py`

Helpers:
* **`RecordingLM`** is a `MockLM` that keeps a copy of every root
  conversation it's sent, so tests can check what the LM saw.
* **`FakeEnv`** has `ContainerManager`'s methods:
  - `execute_code` records the code and returns `REPLResult`s from
    `results`, or raises one that is an exception. By default it returns
    `stdout = "ran: <code>"`, and sets `final_answer = "done"` when the code
    contains `answer["ready"] = True`.
  - It can sleep (`delay`), and it records the most blocks ever running at
    once.
  - `update_handler_address` records each address; `cleanup` counts calls.
* **The `envs` fixture** replaces `Client._new_env` with
  `monkeypatch.setattr`, and returns the list of every `FakeEnv` created.
  Setting `envs.options = dict(...)` configures the next ones.
* `FINISH` is a reply whose block sets the answer to `"done"`;
  `block(code)` wraps code in a reply.

| Test | Checks |
|---|---|
| **The loop** | |
| `test_returns_final_answer` | one `FINISH` reply: `completion` returns `"done"`, with one LM call |
| `test_first_conversation_is_system_then_user` | the LM first sees `system`, then the user's prompt |
| `test_multiple_iterations` | replies `x = 1`, `print(x)`, `FINISH`: three calls; the last conversation alternates roles and holds both earlier results |
| `test_blocks_run_in_order` | one reply with blocks `a` and `b`: run in that order |
| `test_one_feedback_message_per_reply` | the results of two blocks arrive in one `user` message, in order |
| `test_stops_at_first_final_answer` | a block after the answering block never runs |
| `test_no_code_block_gets_reminder` | a prose-only reply: the next conversation ends with `NO_CODE_REMINDER` |
| `test_errors_reach_the_lm` | stderr from a block appears in the next conversation |
| `test_max_iterations_fallback` | `max_iterations=2` and no answer: a third call, ending with `FINAL_ANSWER_REQUEST`; its plain-text reply is returned |
| `test_max_iterations_fallback_runs_answer_block` | `max_iterations=1`, then a `FINISH` reply to the request: `"done"` |
| `test_lm_calls_go_through_the_handler` | `LMHandler.completion` is called once per iteration |
| **Resource lifetimes** | |
| `test_non_persistent_new_env_each_round` | two rounds: two `FakeEnv`s, each cleaned up once |
| `test_cleanup_happens_after_the_round_ends` | the env is cleaned up once, after both blocks ran |
| `test_persistent_env_created_on_enter` | right after `__enter__`, one `FakeEnv` exists, with no handler address yet |
| `test_persistent_reuses_env` | two rounds in a `with` block: one `FakeEnv`, two different handler addresses |
| `test_persistent_cleanup_on_close` | not cleaned up between rounds; cleaned up once when the `with` block ends |
| `test_close_is_idempotent` | two `close()` calls: one cleanup |
| `test_persistent_without_with` | persistent rounds without `with` share one env; `close()` removes it |
| `test_handler_stopped_after_round` | connecting to the round's handler address afterwards is refused |
| `test_cleanup_on_exception` | `execute_code` raises: `completion` raises too, and the env was still cleaned up |
| `test_failed_container_start_leaves_no_handler` | `_new_env` raises: every `LMHandler` that was started was stopped (none was started) |
| `test_persistent_rounds_take_turns` | two threads on one persistent Client: never two blocks running at once |
| `test_non_persistent_rounds_run_in_parallel` | four threads, non-persistent, blocks that take 0.3 s: done in under half of 4 × 0.3 s |
| **Construction** | |
| `test_invalid_tools_rejected_early` | `custom_tools={"answer": 1}` raises `ValueError` in `__init__`, and no env is created |
| `test_max_iterations_must_be_positive` (0, -1) | `ValueError` |
| `test_package_exports` | `dockerrepl.Client`, `dockerrepl.MockLM` and `dockerrepl.BaseLM` |
| **Prompts** | |
| `test_system_prompt_mentions_helpers_and_tools` | ```` ```repl ````, `llm_query`, `llm_query_batched`, `SHOW_VARS`, `answer["ready"] = True`, "persist", and the tool line `- triple: triples` |
| `test_system_prompt_without_tools` | no custom-tools section |
| `test_system_prompt_example_blocks_parse` | `find_code_blocks` finds exactly the prompt's two examples |
| `test_format_result_shows_code_output_and_errors` | the code block, then stdout, then stderr |
| `test_format_result_without_output` | `(no output)`, and no `Errors:` section |
| `test_format_result_truncates` | 20,000 characters each of stdout and stderr: both cut, with two `[truncated` notes |

## Run

```bash
pytest -q tests/test_client.py
```

## Done when

- [ ] All 33 test cases pass **without Docker**, and steps 0–8 still pass.
- [ ] The order in `completion` matches `ARCHITECTURE.md` §3.6 and the §4
      sequence diagram.
- [ ] Each mistake in the Pitfalls table, made on purpose, makes the listed
      test fail. Undo it afterwards.
- [ ] The **Tests** workflow on GitHub shows step 9 as ✅.

## Pitfalls

Each row was checked by making the mistake on purpose:

| Mistake | What goes wrong | Caught by |
|---|---|---|
| **Starting the LMHandler before the container** | if the container fails to start, the handler is never stopped | `test_failed_container_start_leaves_no_handler` |
| **Cleaning up a persistent env at the end of each round** | variables are lost, and the next round fails | `test_persistent_cleanup_on_close`, `test_persistent_without_with` |
| **Cleaning up without `finally`** | an exception, or an early `return` with the answer, leaks a non-persistent container | `test_cleanup_on_exception` and two others |
| **Calling `self.lm.completion` directly** instead of `h.completion` | it works, but bypasses the handler the design puts in the middle | `test_lm_calls_go_through_the_handler` |
| **One `user` message per block** | two `user` messages in a row, which some LM APIs reject | `test_one_feedback_message_per_reply` |
| **Running the blocks after the answer** | code runs after the round should have ended | `test_stops_at_first_final_answer` and others |
| **No reminder after a reply without code** | the LM gets no reply, so the next message isn't from the user | `test_no_code_block_gets_reminder` |
| **A fallback that ignores answer blocks** | the code text is returned as the answer | `test_max_iterations_fallback_runs_answer_block` |
| **No lock for persistent rounds** | two threads run code in one container at once | `test_persistent_rounds_take_turns` |
| **A lock for every round** | independent non-persistent rounds wait for each other | `test_non_persistent_rounds_run_in_parallel` |
| **Validating tools only when the container starts** | a typo is found only at the first `completion` | `test_invalid_tools_rejected_early` |
| **No truncation** | one noisy print fills the LM's context | `test_format_result_truncates` |
| **`__enter__` starting nothing** | the first round pays for the container start, and errors show up late | `test_persistent_env_created_on_enter` |
| **`close()` keeping `_env`** | a second `close()` cleans up twice | `test_close_is_idempotent` |
| **`range(max_iterations + 1)`** | one LM call too many | `test_max_iterations_fallback` |
| **Not checking `max_iterations`** | `max_iterations=0` asks for a final answer before doing anything | `test_max_iterations_must_be_positive` |
