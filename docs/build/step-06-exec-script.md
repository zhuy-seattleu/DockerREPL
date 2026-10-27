# Step 6 — The generated `_exec.py` script

## Goal

A function that turns one LM-written code block into a complete, standalone
Python script. In step 8 the script runs inside the container. In this step
it runs **on your own machine** as a subprocess, with a temporary directory
standing in for `/workspace`, so the whole REPL behaviour can be tested
without Docker.

The script:
1. Loads saved variables from `state.dill`.
2. Defines the helpers (`llm_query`, `llm_query_batched`, `SHOW_VARS`) and
   `answer`.
3. Loads the custom tools and runs the code, with stdout and stderr captured.
4. Saves the variables, using an atomic file replace.
5. Prints one JSON line with the results.

## Tasks at a glance

1. **Write the template** for `_exec.py`. Every name the script itself uses
   starts with `_`, because the user's code runs in the script's own
   namespace (see "The namespace" below).
2. **Load state** from `state.dill`, and start fresh, with a warning, if the
   file is corrupt.
3. **Define the helpers.** `llm_query` and `llm_query_batched` POST to the
   proxy and return `"Error: ..."` strings instead of raising. `SHOW_VARS`
   lists the user's variables.
4. **Load the tools and run the code** with `io.StringIO` buffers for stdout
   and stderr. Report any exception, including `SystemExit`, in stderr, and
   carry on.
5. **Save state atomically:** write a temp file, then `os.replace` it.
6. **Print the result line** after the redirect ends, with output and
   `repr`s cut to a maximum length.
7. **`_build_exec_script`** fills in the template. The code goes in
   base64-encoded; everything else goes in through `repr()`.
8. **Write the unit tests.** `tests/test_exec_script.py` has 47 tests,
   including a fake proxy for `llm_query`.
9. **Check that the tests catch real bugs** by making each mistake in the
   Pitfalls table on purpose.

**Outcome:** `pytest -q` shows 47 more passing tests, and the CI table shows
step 6 as ✅.

## What you'll learn

* **Code generation:** building a program as a string, and why embedding
  untrusted text with base64 is safer than pasting it in directly.
* **`exec()` and namespaces:** what a "REPL" really keeps between runs, and
  why the namespace has to be a real module's for saved functions to work.
* **Serialising state with `dill`,** skipping what can't be pickled.
* **Atomic file writes:** write a temporary file, then `os.replace`.
* **Capturing output:** `io.StringIO` with `contextlib.redirect_stdout` and
  `redirect_stderr`, and what that can't capture.
* **Tracebacks:** naming code with `compile(..., "<repl>", ...)`, showing its
  lines with `linecache`, and hiding the script's own frame.

## Depends on

Step 2 (`DEFAULT_TIMEOUT`) and step 5 (`parse_tool_entry`, `tool_kind`).

## Files

| File | Purpose |
|---|---|
| `dockerrepl/environments/exec_script.py` | the template, `_tool_specs`, `_build_exec_script` |
| `tests/test_exec_script.py` | tests below |

## Interface

```python
HELPER_NAMES: frozenset[str] = frozenset({"llm_query", "llm_query_batched", "SHOW_VARS"})
MAX_OUTPUT_CHARS: int = 20_000   # longer stdout/stderr keep their first and last 10,000 characters
MAX_REPR_CHARS: int = 500        # longest repr() per variable in "locals"

def _tool_specs(custom_tools: dict[str, Any] | None) -> list[tuple[str, str, str]]: ...
    # (kind, name, source): source is JSON text for data, Python source for code and lambdas

def _build_exec_script(code: str,
                       proxy_url: str,
                       custom_tools: dict[str, Any] | None = None,
                       workspace: str = "/workspace",
                       new_round: bool = False) -> str: ...
```

* **`proxy_url`** is the full URL. In the container it is
  `http://host.docker.internal:<port>`. In tests it is
  `http://127.0.0.1:<port>`, or any closed port.
* **`workspace`** is where `state.dill` lives. In tests it is pytest's
  `tmp_path`.
* **`new_round`** is `True` for the first code block of a round. The script
  then replaces any saved `answer` with a fresh one, so an old `ready=True`
  can't end the new round at once. The ContainerManager sets it in step 8.

Taking the full URL and the workspace path as arguments is what lets the same
script run on your machine in tests and inside the container in step 8.

**The JSON line the script prints** is the last line of stdout:

```json
{"stdout": "...", "stderr": "...", "locals": {"x": "1"}, "final_answer": null}
```

## The namespace: why `globals()`, not a dict

The obvious design keeps the REPL's variables in a dict, `ns`, and runs
`exec(code, ns)`. **That breaks functions.** A function's `__globals__` is
the dict it was defined in, and when dill saves the function it has to save
that dict too:

| Run 1 | Run 2 | With `exec(code, ns)` | With `exec(code, globals())` |
|---|---|---|---|
| `x = 1`<br>`def f(): return x` | `x = 5`<br>`print(f())` | prints `1`: `f` reads a frozen copy of the old `ns` | prints `5` |
| same, plus `g = (i for i in range(2))` | `print(f())` | `NameError`: `f` couldn't be saved, because its copy of `ns` holds the generator | works |

For a function defined in `__main__`, dill saves only a *reference* to
`__main__`'s globals. When the next run loads the function, that reference
points at the new script's globals, which hold today's variables and helpers.
So the user's code runs in the script's own namespace,
`exec(compile(code, "<repl>", "exec"), globals())`.

The cost is that the script's own names live in the same namespace as the
user's. So **every internal name starts with `_`**: `_json`, `_PROXY`,
`_STATE`, `_out`, and so on, including the imports (`import json as _json`).
Names starting with `_` are never saved or shown, and the user's `import json`
can't clash with the script's.

## Capturing output

There are two levels:

1. **Inside the script**, two `io.StringIO` buffers capture stdout and
   stderr:
   ```python
   _out, _err = _io.StringIO(), _io.StringIO()
   with _contextlib.redirect_stdout(_out), _contextlib.redirect_stderr(_err):
       ...                      # load the tools, run the code
   # Back to the real stdout:
   _sys.stdout.write("\n" + _json.dumps({"stdout": _out.getvalue(), ...}) + "\n")
   ```
2. **Between the script and the host**, the host runs the script with
   `subprocess.run(..., capture_output=True)` (`docker exec` in step 8). It
   gets the script's real stdout and reads the **last line** as the result.

**What `StringIO` can't capture.** `redirect_stdout` only replaces the
`sys.stdout` object. Output that goes straight to file descriptor 1 or 2
skips it, for example `os.write(1, b"...")`, `subprocess.run(["ls"])` or a C
extension that prints. That output goes to the real stdout, **before** the
JSON line. Nothing breaks, because the host reads only the last line, but it
isn't in the script's `stdout` field. The ContainerManager in step 8 puts it
back: everything before the JSON line goes in front of the `stdout` field it
returns. The newline written before the JSON matters here: without it,
`os.write(1, b"raw")` would put `raw{"stdout": ...` on one line, and the host
couldn't parse it.

## Implementation guide

Build the script from one template string with `str.format`, doubling every
literal `{` and `}` in it. In order, the generated script must:

1. **Import** `base64, contextlib, io, json, linecache, os, sys, traceback,
   dill, requests`, each as `_name` (`import json as _json`).
2. **Set constants**, each written with `repr()` (`{proxy_url!r}` in the
   template) so quotes in the values are safe:
   - `_PROXY`, and `_STATE = _os.path.join(<workspace>, "state.dill")`;
   - `_LLM_TIMEOUT = DEFAULT_TIMEOUT`, `_MAX_OUTPUT`, `_MAX_REPR`;
   - `_TOOLS`, the list from `_tool_specs`, and `_HIDDEN`: the helper names,
     `"answer"` and the tool names;
   - `_CODE = _base64.b64decode("<b64>").decode("utf-8")`. The host builds
     `<b64>` with `base64.b64encode(code.encode("utf-8")).decode("ascii")`.
3. **Load state.** If `_STATE` exists, `dill.load` it and
   `globals().update(...)` with it. If loading fails, or the result isn't a
   dict, keep going with a warning such as
   `"Warning: could not load saved variables (...); starting fresh."`. It is
   written to stderr once the buffers exist.
4. **Define the helpers**, after loading, so they replace any saved copies:
   - Make one `requests.Session()` with **`trust_env = False`**. Otherwise a
     proxy variable such as `HTTP_PROXY`, common on company machines and
     passed into containers by Docker Desktop, would send the call to
     `host.docker.internal` through the corporate proxy.
   - `llm_query(prompt, model=None) -> str` posts `{"prompt", "model"}` to
     `_PROXY + "/llm_query"` with `timeout=_LLM_TIMEOUT`. It returns
     `d["response"]`, or `f"Error: {d['error']}"` if the reply carries an
     error. On any exception it returns `f"Error: {e}"`.
   - `llm_query_batched(prompts, model=None) -> list[str]` works the same way
     with `"/llm_query_batched"` and `{"prompts", "model"}`. On an error
     reply, an exception, or a list of the wrong length, it returns one
     `"Error: ..."` per prompt.
   - `SHOW_VARS() -> str` returns `"Available variables: {name: type_name, ...}"`
     for the user's variables: names not starting with `_` and not in
     `_HIDDEN`. If there are none it returns `"No variables created yet."`.
5. **Set `answer = {"content": "", "ready": False}`** only if `new_round` is
   set, or `answer` is missing or isn't a dict. Otherwise keep the saved one,
   so the LM can build an answer over several blocks.
6. **Run, inside `redirect_stdout(_out)` and `redirect_stderr(_err)`:**
   - Load each tool from `_TOOLS`: code with `exec(source, globals())`, a
     lambda with `globals()[name] = eval(source, globals())`, data with
     `globals()[name] = json.loads(source)`. `exec` would create a lambda and
     throw it away, so the name would never be defined. Wrap each tool in
     `try`: on failure, write `"Error loading tool 'name':"` and the
     traceback to stderr, and carry on.
   - Add `_CODE` to `linecache.cache` under `"<repl>"`, then run
     `exec(compile(_CODE, "<repl>", "exec"), globals())`. Tracebacks then
     say `File "<repl>", line 2` and show that line.
   - Catch `(Exception, SystemExit)`. A `SyntaxError` from `compile` is
     caught here too. Print the traceback to stderr, skipping the first frame
     (`exc.__traceback__.tb_next`), which is the script's own `exec` line.
     Don't stop: variables set before the error are kept, as in a normal
     REPL.
7. **Save state atomically.** Build `_clean` from the user's variables,
   keeping only values where `dill.dumps(v)` works, plus `answer` if it's a
   dict. Write it to `_STATE + ".tmp"`, then
   `os.replace(_STATE + ".tmp", _STATE)`. If saving fails, write a warning to
   stderr.
8. **Print the result line**, after the `with` block, as
   `"\n" + json.dumps({...}) + "\n"`:
   - `stdout` and `stderr`: the buffers, cut with `_cap` (keep the first and
     last half of `_MAX_OUTPUT`, with `[... N characters cut ...]` between);
   - `locals`: `{name: repr(value)}` for the saved user variables, each cut
     at `_MAX_REPR` characters, with `"<repr failed: Type>"` if `repr`
     raises;
   - `final_answer`: `str(answer["content"])` if `answer` is a dict and
     `answer["ready"]` is true, else `None`.

`_tool_specs` returns `(tool_kind(value), name, source)` for each tool, after
`parse_tool_entry`. The source is `json.dumps(value)` for data and
`value.strip()` for code and lambdas. The list is embedded with `repr()`. It
holds only strings, so `repr()` writes it as a valid Python literal.

## Tests — `tests/test_exec_script.py`

A helper in the test file builds the script, runs it with the test's
`tmp_path` as the workspace, and returns the parsed last line:

```python
def run_script(tmp_path, code, proxy_url=CLOSED_PROXY, custom_tools=None, new_round=False) -> dict:
    script = _build_exec_script(code, proxy_url, custom_tools, workspace=str(tmp_path), new_round=new_round)
    path = tmp_path / "_exec.py"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(path)], capture_output=True,
                          text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])
```

`CLOSED_PROXY` is `http://127.0.0.1:9`. Port 9 (the "discard" port) is almost
always closed, so `llm_query` fails fast there. To test real replies,
`FakeProxy` is a tiny `ThreadingHTTPServer` that answers every POST with a
fixed JSON reply and records each request's path and body. It stands in for
the real proxy of step 7.

| Test | Checks |
|---|---|
| **Output** | |
| `test_prints_stdout` | `print("hi")` gives `stdout == "hi\n"` |
| `test_unicode_output` | `print("héllo ✓")` comes back exactly |
| `test_stderr_captured` | a print to `sys.stderr` lands in `stderr`, not `stdout` |
| `test_exception_goes_to_stderr` | `1/0` puts `ZeroDivisionError` in stderr, the script still prints its JSON line, and `final_answer is None` |
| `test_traceback_points_at_user_code` | the traceback says `File "<repl>", line 2`, shows that line, and doesn't mention `_exec.py` |
| `test_syntax_error_reported` | `def f(:` gives a `SyntaxError` naming `<repl>` |
| `test_sys_exit_still_reports` | `sys.exit(3)` after a print: the print is kept and stderr mentions `SystemExit` |
| `test_raw_write_without_newline` | `os.write(1, b"raw")` doesn't spoil the JSON line |
| `test_long_output_cut` | 100,000 characters come back as at most about `MAX_OUTPUT_CHARS`, keeping the start and the end |
| **State** | |
| `test_variables_persist_between_runs` | run 1: `x = 41`; run 2: `print(x + 1)` gives `"42\n"` |
| `test_partial_state_kept_after_error` | `a = 1; 1/0; b = 2`, then `a` is there and `b` gives `NameError` |
| `test_unpicklable_value_skipped` | a generator isn't saved, but the variable next to it is |
| `test_function_sees_current_variables` | run 1 defines `f` reading `x = 1`; run 2 sets `x = 5` and `f()` gives `5` |
| `test_function_kept_next_to_unpicklable_value` | `f` survives even though a generator was defined alongside it |
| `test_saved_function_can_call_helpers` | a saved function calling `llm_query` still works in the next run |
| `test_class_and_instance_persist` | a class and an instance of it both survive |
| `test_import_persists` | `import math`, then `math.sqrt(16)` in the next run |
| `test_state_written_atomically` | after a run, `state.dill` exists and `state.dill.tmp` doesn't |
| `test_failed_save_keeps_old_state` | a value that passes the `dumps` check but fails during the real save: stderr warns, and the next run still sees the old `x` |
| `test_corrupt_state_starts_fresh` | garbage in `state.dill`: no variables, and a warning in stderr |
| **`locals` and `SHOW_VARS`** | |
| `test_locals_reports_repr` | `z = [1, 2]` gives `locals["z"] == "[1, 2]"` |
| `test_long_repr_cut` | a 100,000-item list's repr is cut to `MAX_REPR_CHARS` plus `...` |
| `test_bad_repr_doesnt_crash` | a `__repr__` that raises gives `"<repr failed: RuntimeError>"` |
| `test_locals_hide_helpers_tools_and_internals` | with one tool, `x = 1` gives exactly `{"x"}` |
| `test_show_vars_lists_user_variables` | mentions `'x': 'int'` and `'s': 'str'`, not `llm_query`, `answer` or `_` names |
| `test_show_vars_when_empty` | a fresh workspace gives `"No variables created yet."` |
| **`answer`** | |
| `test_final_answer` | `answer["content"] = "done"; answer["ready"] = True` gives `final_answer == "done"` |
| `test_no_final_answer_by_default` | `x = 1` gives `final_answer is None` |
| `test_answer_survives_between_runs` | run 1 sets `answer["content"] = "partial"` without `ready`; run 2 prints `"partial"` |
| `test_new_round_resets_answer` | after `ready = True` is saved, a run with `new_round=True` sees a fresh `answer`; other variables are kept |
| `test_new_round_only_resets_once` | a later run without `new_round` keeps the answer the first run started |
| `test_answer_replaced_when_not_a_dict` | `answer = 5` gives no final answer, and the next run has a fresh `answer` dict |
| **Embedding** | |
| `test_tricky_code_is_safe` | code containing `'''`, `"""`, a backslash and `{}` runs and prints exactly as written |
| `test_code_is_never_pasted_in` | a marker string in the code doesn't appear in the script's text |
| **Custom tools** | |
| `test_custom_code_tool_available` | the tool `{"triple": "def triple(x):\n    return x * 3"}` makes `print(triple(5))` give `"15\n"` |
| `test_custom_lambda_tool_available` | the tool `{"double": "lambda x: 2 * x"}` makes `print(double(4))` give `"8\n"` |
| `test_custom_data_tool_available` | an int, a wrapped dict and the one-line string `"x = 5"` (data, not code) |
| `test_tools_reinjected_after_overwrite` | run 1: `CONST = 0`; run 2: `print(CONST)` gives `"7\n"` |
| `test_helpers_reinjected_after_overwrite` | run 1: `llm_query = None`; run 2: `print(callable(llm_query))` gives `"True\n"` |
| `test_broken_tool_reported_and_code_still_runs` | a tool importing a missing module: stderr names the tool and `ModuleNotFoundError`, and the code still runs |
| **`llm_query`** | |
| `test_llm_query_unreachable_returns_error_string` | `llm_query("x")` returns `"Error: ..."`, and stderr stays empty |
| `test_llm_query_batched_unreachable` | two prompts give two `"Error: ..."` strings |
| `test_llm_query_posts_to_proxy` | with `FakeProxy`: returns `"pong"` and posts `/llm_query` with `{"prompt": "hi", "model": None}` |
| `test_llm_query_error_reply` | an `{"error": "boom"}` reply gives `"Error: boom"` |
| `test_llm_query_batched_posts_to_proxy` | posts `/llm_query_batched` with the prompts and model, and returns the list as sent |
| `test_llm_query_batched_wrong_count` | one response for two prompts gives two `"Error: ..."` strings |
| `test_llm_query_ignores_proxy_environment` | with `HTTP_PROXY` pointing at a closed port, `llm_query` still reaches `FakeProxy` |

## Run

```bash
pytest -q tests/test_exec_script.py
```

Each test starts one or more Python processes, so the file takes about
10–15 seconds.

## Done when

- [ ] All 47 tests pass, and steps 0–5 still pass.
- [ ] The printed JSON line is always the **last** line of stdout, even when
      user code prints a lot or writes to the real stdout.
- [ ] `_build_exec_script` never pastes `code` into the template unencoded.
- [ ] Each mistake in the Pitfalls table, made on purpose, makes the listed
      test fail. Undo it afterwards.
- [ ] The **Tests** workflow on GitHub shows step 6 as ✅.

## Pitfalls

Each row was checked by making the mistake on purpose:

| Mistake | What goes wrong | Caught by |
|---|---|---|
| **Running the code in a separate `ns` dict** | a saved function keeps reading old values of the globals it uses, and is lost if it was defined in the same run as a value that can't be pickled. Functions that use only their arguments still work, which makes the bug easy to miss | `test_function_sees_current_variables`, `test_function_kept_next_to_unpicklable_value` |
| **Pasting `code` straight into the template** | one `'''` in LM-written code breaks the script, or worse, injects code | `test_code_is_never_pasted_in`, `test_tricky_code_is_safe` |
| **Printing the result JSON while stdout is still redirected** | it ends up inside the captured `stdout` | `test_prints_stdout` and others |
| **No newline before the JSON** | `os.write(1, b"raw")` and the JSON share a line | `test_raw_write_without_newline` |
| **Catching only `Exception`** | `sys.exit()` or `exit()` ends the script before the JSON line | `test_sys_exit_still_reports` |
| **Writing `state.dill` directly** | a failed or killed save leaves a corrupt file, and all variables are lost | `test_failed_save_keeps_old_state` |
| **Not catching load errors** | a corrupt `state.dill` breaks every later run | `test_corrupt_state_starts_fresh` |
| **Saving the helpers and loading state after defining them** | `llm_query = None` survives into the next run | `test_helpers_reinjected_after_overwrite` and others |
| **Letting `requests` read proxy settings from the environment** | on a company machine, calls to `host.docker.internal` go to the corporate proxy | `test_llm_query_ignores_proxy_environment` |
| **Loading a lambda tool with `exec`** | the name is never defined | `test_custom_lambda_tool_available` |
| **Not skipping the script's frame** | tracebacks start with `_exec.py` | `test_traceback_points_at_user_code` |
| **No output cap** | a print loop makes a huge result line | `test_long_output_cut` |
| **Always resetting `answer`** | a partial answer is lost between runs | `test_answer_survives_between_runs` |
| **Not catching tool errors** | one broken tool stops every run | `test_broken_tool_reported_and_code_still_runs` |
| **Not checking the batched count** | a short reply gives too few results | `test_llm_query_batched_wrong_count` |
| **Calling `repr` unguarded** | one bad `__repr__` crashes the script | `test_bad_repr_doesnt_crash` |
| **Leaving tool names out of `_HIDDEN`** | tools show up in `locals` and `SHOW_VARS` | `test_locals_hide_helpers_tools_and_internals` |

One more thing to know: user code can still overwrite the script's internal
names, for example by assigning `_STATE = 0`. Names starting with `_` are the
script's by convention. That's acceptable for an MVP whose code comes from an
LM following its system prompt.
