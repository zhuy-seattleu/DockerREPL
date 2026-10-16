# Step 9 — `Client` (the round loop)

## Goal

The class users talk to. It owns the session's container (persistent mode)
or creates one per round (non-persistent mode). Each round it starts a new
LMHandler, points the container at it, and runs the iteration loop: ask the
LM, run its code blocks, feed the results back, and stop at a final answer
or after `max_iterations`.

It is tested **without Docker**, by replacing the container with a fake
environment. The real container is used in step 10.

## What you'll learn

* **Orchestration:** putting components together in the right order
  (container → handler → iterations → handler stop → container cleanup).
* **Resource lifetimes:** session scope vs round scope, and `try/finally` so
  cleanup always happens.
* **Test doubles:** a `FakeEnv` with the same interface as
  `ContainerManager`, injected with `monkeypatch`.
* **Prompt construction:** a system prompt that teaches the LM the REPL
  conventions.

## Depends on

Step 1 (`find_code_blocks`), step 2 (`MockLM`), step 4 (`LMHandler`), step 5
(`format_tools_for_prompt`) and step 8 (`ContainerManager`, `REPLResult`).

## Files

| File | Purpose |
|---|---|
| `dockerrepl/client.py` | prompt helpers, `Client` |
| `dockerrepl/__init__.py` | re-export `Client`, `MockLM`, `BaseLM` |
| `tests/test_client.py` | tests below |

## Interface

```python
# Messages (step 2) = list[dict[str, Any]]; each dict is {"role": "system" | "user" | "assistant", "content": str}

def build_system_prompt(custom_tools: dict[str, Any] | None) -> Messages: ...
def format_result(code: str, result: REPLResult) -> str: ...
FINAL_ANSWER_REQUEST: dict[str, Any]   # user message asking for a final answer

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

1. **`build_system_prompt`** returns `[{"role": "system", "content": text}]`.
   The text explains:
   - how to write ```` ```repl ```` blocks;
   - that variables persist between blocks;
   - `llm_query(prompt)`, `llm_query_batched(prompts)` and `SHOW_VARS()`;
   - how to finish, by setting `answer["content"]` and then
     `answer["ready"] = True`;
   - and, if there are any, the tools from `format_tools_for_prompt`.
2. **`format_result`** turns one run into text for the LM: the code, then
   `stdout`, then `stderr` if not empty. Cut each output down to 5,000
   characters with a `"... [truncated]"` note, so one noisy print can't fill
   the context.
3. **`__init__`** validates `custom_tools` with step 5 and checks that
   `max_iterations >= 1`. It does **not** start anything.
4. **`__enter__`** starts the container (`self._env = self._new_env()`) if
   `persistent`, and returns `self`. **`__exit__`** calls `close()`.
5. **`_new_env`** returns
   `ContainerManager(self.image, persistent=self.persistent, custom_tools=self.custom_tools)`.
   Keeping it a separate method is what lets tests replace it.
6. **`completion(prompt)`:** follow the sketch in `ARCHITECTURE.md` §3.6
   exactly:
   1. Pick the environment: the session's (created if `None`) or a new one.
   2. `with LMHandler(self.lm) as h:` then
      `env.update_handler_address(h.address)`.
   3. Loop up to `max_iterations` times:
      - `reply = h.completion(messages)` and append it as `assistant`.
      - For each block from `find_code_blocks(reply)`: `env.execute_code`. If
        `final_answer` is set, return it. Otherwise append
        `format_result(...)` as a `user` message.
      - **If the reply had no code blocks,** append a `user` message
        reminding the LM to use a `repl` block or set `answer`.
   4. After the loop:
      `return h.completion(messages + [FINAL_ANSWER_REQUEST])`.
   5. `finally:` if not persistent, `env.cleanup()`.
7. **`close()`** cleans up `_env` if it exists, then sets it to `None`. It is
   safe to call twice.

## Tests — `tests/test_client.py`

`FakeEnv` has the same methods as `ContainerManager`:
* `execute_code` records the code and returns `REPLResult`s from a queue. By
  default the result echoes the code in `stdout`. It sets
  `final_answer = "done"` when the code contains `answer["ready"] = True`.
* `update_handler_address` records each address.
* `cleanup` adds one to a counter.

The fixture `fake_envs` uses
`monkeypatch.setattr(Client, "_new_env", lambda self: make_fake())` and
collects every `FakeEnv` created.

| Test | Checks |
|---|---|
| `test_returns_final_answer` | `MockLM` scripted with one reply whose block sets the answer: `completion` returns `"done"`, and the LM was called once |
| `test_multiple_iterations` | replies: (1) `x = 1`, (2) `print(x)`, (3) the answer: three iterations, and both earlier results appear in the messages the LM received |
| `test_blocks_run_in_order` | one reply with two blocks `a` and `b`: `FakeEnv` recorded them in that order |
| `test_stops_at_first_final_answer` | a reply whose first block sets the answer and whose second block prints: the second block never runs |
| `test_no_code_block_gets_reminder` | a reply with only prose: the next LM call's messages end with the reminder |
| `test_max_iterations_fallback` | `max_iterations=2` and replies that never finish: the LM is called 3 times, and the last call includes `FINAL_ANSWER_REQUEST` |
| `test_non_persistent_new_env_each_round` | two `completion` calls: 2 `FakeEnv`s, each cleaned up once |
| `test_persistent_reuses_env` | `with Client(..., persistent=True)` and two calls: 1 `FakeEnv`, with 2 different handler addresses recorded |
| `test_persistent_env_created_on_enter` | right after `__enter__`, before any call, one `FakeEnv` exists |
| `test_persistent_cleanup_on_close` | not cleaned up after the calls; cleaned up once after the `with` block ends |
| `test_handler_stopped_after_round` | connecting to the recorded handler address after `completion` returns is refused |
| `test_cleanup_on_exception` | `FakeEnv.execute_code` raises: `completion` raises too, and the non-persistent env was still cleaned up |
| `test_system_prompt_mentions_helpers_and_tools` | `build_system_prompt({"triple": {"tool": "def triple(x): ...", "description": "triples"}})` contains `llm_query`, `llm_query_batched`, `SHOW_VARS`, `answer["ready"]`, `triple` and `triples` |
| `test_format_result_truncates` | a 20,000-character stdout is cut down to about 5,000 characters plus `"[truncated]"` |
| `test_invalid_tools_rejected_early` | `Client(MockLM(), custom_tools={"answer": 1})` raises `ValueError`, and no env is created |

## Run

```bash
pytest -q tests/test_client.py
```

## Done when

- [ ] All tests pass **without Docker**, and steps 0–8 still pass.
- [ ] The order in `completion` matches `ARCHITECTURE.md` §3.6 and the §4
      sequence diagram.

## Pitfalls

* **Creating the LMHandler before the container.** It works, but it breaks
  the session/round split. `test_persistent_env_created_on_enter` and the
  address-recording tests pin down the intended order.
* **Cleaning up a persistent env at the end of each round.** Variables are
  lost and the next round fails. `test_persistent_cleanup_on_close` catches
  it.
* **Returning from inside `with LMHandler(...)` without `finally`.** A
  non-persistent container is leaked whenever a final answer comes early.
* **Calling `self.lm.completion` directly** instead of `h.completion`. It
  works, but bypasses the handler the design puts in the middle. Keep one
  path.
