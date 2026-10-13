# Step 6 — The generated `_exec.py` script

## Goal

A function that turns one LM-written code block into a complete, standalone
Python script. In step 8 the script runs inside the container. In this step
it runs **on your own machine** as a subprocess, with a temporary directory
standing in for `/workspace`, so the whole REPL behaviour can be tested
without Docker.

The script:
1. Loads saved variables from `state.dill`.
2. Adds the helpers (`llm_query`, `llm_query_batched`, `SHOW_VARS`), the
   custom tools and `answer`.
3. Runs the code with its output captured.
4. Saves the variables, using an atomic file replace.
5. Prints one JSON line with the results.

## What you'll learn

* **Code generation:** building a program as a string, and why embedding
  untrusted text with base64 is safer than pasting it in directly.
* **`exec()` and namespaces:** what a "REPL" really keeps between runs.
* **Serialising state with `dill`,** skipping what can't be pickled.
* **Atomic file writes:** write a temporary file, then `os.replace`.
* **Capturing output:** `contextlib.redirect_stdout` and `redirect_stderr`.

## Depends on

Step 2 (`DEFAULT_TIMEOUT`) and step 5 (`parse_tool_entry`, `is_code_string`).

## Files

| File | Purpose |
|---|---|
| `dockerrepl/environments/exec_script.py` | `_build_custom_tools_code`, `_build_exec_script` |
| `tests/test_exec_script.py` | tests below |

## Interface

```python
HELPER_NAMES: frozenset[str] = frozenset({"llm_query", "llm_query_batched", "SHOW_VARS"})

def _build_custom_tools_code(custom_tools: dict[str, Any] | None) -> str: ...

def _build_exec_script(code: str,
                       proxy_url: str,
                       custom_tools: dict[str, Any] | None = None,
                       workspace: str = "/workspace") -> str: ...
```

* **`proxy_url`** is the full URL. In the container it is
  `http://host.docker.internal:<port>`. In tests it is
  `http://127.0.0.1:<port>`, or any closed port.
* **`workspace`** is where `state.dill` lives. In tests it is pytest's
  `tmp_path`.

Taking the full URL and the workspace path as arguments is what lets the same
script run on your machine in tests and inside the container in step 8.

**The JSON line the script prints** is the last line of stdout:

```json
{"stdout": "...", "stderr": "...", "locals": {"x": "1"}, "final_answer": null}
```

## Implementation guide

Build the script from a template string with `textwrap.dedent` and
`str.format`, or with f-strings, doubling any literal braces. In order, the
generated script must:

1. **Import** `sys, io, os, json, base64, traceback, contextlib, requests, dill`.
2. **Set constants:**
   - `PROXY = <proxy_url>` and `STATE = os.path.join(<workspace>, "state.dill")`,
     each written with `repr()` so quotes in the values are safe.
   - `_LLM_TIMEOUT = <DEFAULT_TIMEOUT>`.
3. **Define the helpers:**
   - `llm_query(prompt, model=None) -> str` posts `{"prompt", "model"}` to
     `PROXY + "/llm_query"`. It returns `d["response"]`, or
     `f"Error: {d['error']}"` if the reply carries an error. On any exception
     it returns `f"Error: {e}"`.
   - `llm_query_batched(prompts, model=None) -> list[str]` works the same way
     with `"/llm_query_batched"` and `{"prompts", "model"}`. On failure it
     returns `[f"Error: {e}"] * len(prompts)`.
   - `SHOW_VARS() -> str` lists the user variables with their type names:
     names not starting with `_`, not helpers, not `answer`, not
     `__builtins__`. If there are none it returns
     `"No variables created yet."`.
4. **Load state:**
   - `ns = dill.load(...)` if `STATE` exists; otherwise, or if loading
     fails, `ns = {}`.
5. **Inject**, overwriting anything in `ns` with the same name:
   - the helpers;
   - the custom tools, from `_build_custom_tools_code`;
   - `answer = {"content": "", "ready": False}`, **only if** `answer` is
     missing or isn't a dict.
6. **Decode the code** with `code = base64.b64decode("<b64>").decode()`. The
   host builds `<b64>` with `base64.b64encode(code.encode()).decode()`.
7. **Run it:**
   - `exec(code, ns)` inside `redirect_stdout(out)` and
     `redirect_stderr(err)`.
   - On exception, write `traceback.format_exc()` to `err`. Do **not** stop:
     variables set before the error are kept, as in a normal REPL.
8. **Save state atomically.** Build a `clean` dict of the names `SHOW_VARS`
   would list, plus `answer`. Keep only values where `dill.dumps(v)` works.
   `dill.dump(clean, open(STATE + ".tmp", "wb"))`, then
   `os.replace(STATE + ".tmp", STATE)`.
9. **Print the result line:** `print(json.dumps({...}))` with stdout, stderr,
   `locals = {name: repr(value)}` for the saved user variables, and
   `final_answer = str(answer["content"]) if answer.get("ready") else None`.

`_build_custom_tools_code` returns lines of Python for the generated script:

* For a **code string** (`is_code_string`): `exec(<repr(code)>, ns)`.
* For **data**: `ns[<repr(name)>] = json.loads(<repr(json.dumps(value))>)`.

## Tests — `tests/test_exec_script.py`

A helper in the test file:

```python
def run_script(tmp_path, code, proxy_url="http://127.0.0.1:9", custom_tools=None) -> dict:
    script = _build_exec_script(code, proxy_url, custom_tools, workspace=str(tmp_path))
    path = tmp_path / "_exec.py"
    path.write_text(script)
    proc = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=60)
    return json.loads(proc.stdout.strip().splitlines()[-1])
```

Port 9 (the "discard" port) is almost always closed, so `llm_query` fails
fast there.

| Test | Checks |
|---|---|
| `test_prints_stdout` | `print("hi")` gives `stdout == "hi\n"` |
| `test_exception_goes_to_stderr` | `1/0` puts `ZeroDivisionError` in stderr, the script still prints its JSON line, and `final_answer is None` |
| `test_variables_persist_between_runs` | run 1: `x = 41`; run 2: `print(x + 1)` gives `"42\n"` |
| `test_partial_state_kept_after_error` | `a = 1; 1/0` then `print(a)` gives `"1\n"` |
| `test_unpicklable_value_skipped` | `gen = (i for i in range(3)); y = 2` (dill can't pickle a generator), then `print(y)` works and `gen` isn't saved (printing it gives a `NameError` mentioning `gen`) |
| `test_locals_reports_repr` | `z = [1, 2]` gives `locals["z"] == "[1, 2]"` |
| `test_final_answer` | `answer["content"] = "done"; answer["ready"] = True` gives `final_answer == "done"` |
| `test_no_final_answer_by_default` | `x = 1` gives `final_answer is None` |
| `test_answer_survives_between_runs` | run 1 sets `answer["content"] = "partial"` without `ready`; run 2 `print(answer["content"])` gives `"partial"` |
| `test_show_vars_lists_user_variables` | after `x = 1; s = "a"`, `print(SHOW_VARS())` mentions `x` and `int`, and `s` and `str`, but not `llm_query` or `answer` |
| `test_show_vars_when_empty` | a fresh workspace gives `"No variables created yet."` |
| `test_tricky_code_is_safe` | code containing `'''`, `"""`, a backslash and `{}` runs and prints exactly as written |
| `test_custom_code_tool_available` | the tool `{"triple": "def triple(x):\n    return x * 3"}` makes `print(triple(5))` give `"15\n"` |
| `test_custom_data_tool_available` | `{"CONST": 7, "LOOKUP": {"tool": {"a": 1}, "description": "d"}}` gives `print(CONST, LOOKUP["a"])` output `"7 1\n"` |
| `test_tools_reinjected_after_overwrite` | run 1: `CONST = 0`; run 2: `print(CONST)` gives `"7\n"` |
| `test_helpers_reinjected_after_overwrite` | run 1: `llm_query = None`; run 2: `print(callable(llm_query))` gives `"True\n"` |
| `test_llm_query_unreachable_returns_error_string` | `print(llm_query("x"))` starts with `"Error:"` and doesn't raise |
| `test_llm_query_batched_unreachable` | `r = llm_query_batched(["a", "b"])` gives `len(r) == 2` and both start with `"Error:"` |
| `test_state_written_atomically` | after a run, `state.dill` exists and `state.dill.tmp` doesn't |
| `test_corrupt_state_starts_fresh` | write garbage to `state.dill`; `print(SHOW_VARS())` still works and reports no variables |

## Run

```bash
pytest -q tests/test_exec_script.py
```

## Done when

- [ ] All tests pass, and steps 0–5 still pass.
- [ ] The printed JSON line is always the **last** line of stdout, even when
      user code prints a lot.
- [ ] `_build_exec_script` never pastes `code` into the template unencoded.

## Pitfalls

* **Pasting `code` straight into the template.** One `'''` in LM-written code
  breaks the script, or worse, injects code. Always base64-encode it.
* **Printing the result JSON while stdout is still redirected.** It then ends
  up inside the captured `stdout` field. Print it after the `with` block.
* **Saving `__builtins__`** or the helpers into `state.dill`. They are
  re-injected anyway, and `__builtins__` makes the file large.
* **Writing `state.dill` directly.** A kill mid-write (step 8 adds timeouts)
  would leave a corrupt file. `test_state_written_atomically` checks for the
  temp-file approach.
