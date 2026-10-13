# Step 10 — End-to-end tests and the example script

## Goal

Prove the whole system works together with a real container, and give
students a runnable example. Nothing new is built in this step. It runs
complete conversations, scripted with `MockLM`, through the real `Client`,
`LMHandler`, proxy and Docker container.

## What you'll learn

* **Integration vs unit testing:** what each kind can and can't show.
* **Scripting a deterministic "conversation"** with a mock LM, to test
  multi-step behaviour.
* **Writing an example** that explains itself when run.

## Depends on

All previous steps. Needs a running Docker daemon.

## Files

| File | Purpose |
|---|---|
| `examples/docker_repl_example.py` | runnable demo, `python examples/docker_repl_example.py` |
| `tests/test_e2e.py` | tests below, all `@pytest.mark.docker` |

## The example script

It prints a heading for each part, then what was sent and what came back:

1. **Basic execution.** A `MockLM` scripted with one block: `x = 1 + 2`,
   `print(x)`, then set the answer to `str(x)`.
2. **`llm_query` and `llm_query_batched`.** One block calls both, prints the
   results, and puts them in the answer.
3. **`SHOW_VARS`.** Over two iterations: first create variables, then
   `print(SHOW_VARS())`, then answer.
4. **Custom tools.** `custom_tools={"triple": "def triple(x):\n    return x * 3", "CONST": 7}`
   and a block that answers with `triple(CONST)`, which is 21.
5. **Persistent session.** `with Client(lm, persistent=True) as c:`. Round 1
   stores `saved = llm_query("remember me")`, and round 2 answers with
   `saved`.

Each part builds its own `MockLM(scripted=[...])` with replies written out in
full, so students can read exactly what the "LM" said.

## Tests — `tests/test_e2e.py` (all `@pytest.mark.docker`)

| Test | Scripted LM replies | Expected `completion()` result |
|---|---|---|
| `test_e2e_basic_execution` | one block: `x = 6 * 7` and the answer `str(x)` | `"42"` |
| `test_e2e_multi_iteration_state` | (1) `data = [1, 2, 3]`, (2) answer `str(sum(data))` | `"6"` |
| `test_e2e_llm_query` | one block: `r = llm_query("ping")` and answer `r` | `"Mock: ping"` |
| `test_e2e_llm_query_batched` | one block: answer `"|".join(llm_query_batched(["a", "b", "c"]))` | `"Mock: a\|Mock: b\|Mock: c"` |
| `test_e2e_show_vars` | (1) `alpha = 1`, (2) answer `SHOW_VARS()` | contains `"alpha"` |
| `test_e2e_custom_tools` | answer `str(triple(CONST))` | `"21"` |
| `test_e2e_error_then_recovery` | (1) `1/0`, (2) answer `"recovered"` | `"recovered"`; the second LM call's messages contain `ZeroDivisionError` |
| `test_e2e_persistent_across_rounds` | round 1: `saved = llm_query("remember me")` + answer `"ok"`; round 2: answer `saved` | round 2 returns `"Mock: remember me"` |
| `test_e2e_non_persistent_forgets` | the same two rounds with `persistent=False` | round 2's block fails with `NameError` (visible in the messages), the script runs out, and MockLM's fixed reply ends the round: the result is `"mock final answer"`, not `"Mock: remember me"` |
| `test_e2e_timeout_recovers` | a small `Client` subclass in the test whose `_new_env` passes `exec_timeout=2`: (1) `while True: pass`, (2) answer `"after timeout"` | `"after timeout"`; the messages contain `"timed out"` |
| `test_e2e_no_leftover_containers` | run one non-persistent round | `docker ps -a --filter ancestor=python:3.11-slim` doesn't list that round's container afterwards |

Tip: a short helper `answer_block(expr: str) -> str` returns a full reply
whose `repl` block sets `answer["content"] = <expr>` and
`answer["ready"] = True`. It keeps the scripted replies short.

## Run

```bash
pytest -q -m docker tests/test_e2e.py
python examples/docker_repl_example.py
pytest -q                                # the full suite, every step
```

## Done when

- [ ] All end-to-end tests pass with Docker running.
- [ ] The example runs from start to finish and its output is easy to follow.
- [ ] `pytest -q` passes on a machine **without** Docker, with only the
      `docker`-marked tests skipped.
- [ ] No containers or workspace folders are left behind afterwards.

## Pitfalls

* **Writing scripted replies that don't match `REPL_BLOCK_PATTERN`.** For
  example, a missing newline before the closing fence. Use `answer_block` and
  check new replies with `find_code_blocks` first.
* **Expecting `MockLM` to reason.** It only plays back its script. If a test
  needs the "LM" to react to a result, put the reaction in the next scripted
  reply.
* **Slow test runs.** Each non-persistent round starts a container and runs
  `pip install`. Keep the number of e2e tests small and put detailed cases in
  the unit tests of earlier steps.
