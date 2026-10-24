"""Build `_exec.py`: the standalone script that runs one code block in the REPL.

The script is generated on the host and run as `python _exec.py` (in the
container from step 8 on, as a local subprocess in the tests). It:

1. loads the saved variables from `<workspace>/state.dill`;
2. defines `llm_query`, `llm_query_batched` and `SHOW_VARS`, then the custom
   tools and `answer`;
3. runs the code with stdout and stderr captured;
4. saves the variables again (atomically);
5. prints one JSON line: {"stdout", "stderr", "locals", "final_answer"}.

Why the code runs in `globals()`, not in a separate dict:

* A function looks up its global names (variables, helpers, other
  functions) in the dict it was defined in, `f.__globals__`.
* Each code block runs in a new process, so dill must save `f` and, with it,
  something for `f.__globals__`.
* If that dict is `__main__`'s own, dill saves only a reference to it. When
  the next run loads `f`, the reference points at that run's globals, so `f`
  sees the current variables.
* If it's a separate dict, dill saves a copy of the dict. `f` then keeps
  reading the values from the run that saved it, even after a later run
  changes them. Also, if the copy holds a value that can't be pickled (say a
  generator made in the same run), `f` can't be saved at all.

The script's own names share this namespace, so they all start with `_`.
Names starting with `_` are never saved or shown, and they can't clash with
the user's names (the script uses `_json`, so the user's `json` is safe).
"""

import base64
import json
from typing import Any

from dockerrepl.constants import DEFAULT_TIMEOUT
from dockerrepl.environments.tools import parse_tool_entry, tool_kind

HELPER_NAMES: frozenset[str] = frozenset({"llm_query", "llm_query_batched", "SHOW_VARS"})

# Captured stdout/stderr longer than this keep only their start and end, so a
# runaway print loop can't produce a huge result line.
MAX_OUTPUT_CHARS: int = 20_000
# Longest repr() reported per variable in "locals".
MAX_REPR_CHARS: int = 500

_TEMPLATE = '''\
import base64 as _base64
import contextlib as _contextlib
import io as _io
import json as _json
import linecache as _linecache
import os as _os
import sys as _sys
import traceback as _traceback

import dill as _dill
import requests as _requests

_PROXY = {proxy_url!r}
_STATE = _os.path.join({workspace!r}, "state.dill")
_LLM_TIMEOUT = {llm_timeout!r}
_MAX_OUTPUT = {max_output!r}
_MAX_REPR = {max_repr!r}
_TOOLS = {tools!r}
_TOOL_NAMES = {{_name for _kind, _name, _src in _TOOLS}}
_HIDDEN = {{"llm_query", "llm_query_batched", "SHOW_VARS", "answer"}} | _TOOL_NAMES
_CODE = _base64.b64decode({code_b64!r}).decode("utf-8")

# 1. Load the saved variables into this module's namespace.
_notes = []
if _os.path.exists(_STATE):
    try:
        with open(_STATE, "rb") as _f:
            _saved = _dill.load(_f)
        if not isinstance(_saved, dict):
            raise TypeError("saved state is not a dict")
        globals().update(_saved)
    except Exception as _e:
        _notes.append(f"Warning: could not load saved variables ({{type(_e).__name__}}: {{_e}}); starting fresh.\\n")

# 2. Helpers, defined after loading so they always replace saved copies.
_http = _requests.Session()
_http.trust_env = False  # never send container-to-host calls through a proxy from the environment


def llm_query(prompt, model=None):
    """Ask the LM one prompt; returns its reply, or an "Error: ..." string."""
    try:
        _r = _http.post(_PROXY + "/llm_query", json={{"prompt": prompt, "model": model}}, timeout=_LLM_TIMEOUT)
        _d = _r.json()
        if _d.get("error") is not None:
            return f"Error: {{_d['error']}}"
        return _d["response"]
    except Exception as _e:
        return f"Error: {{_e}}"


def llm_query_batched(prompts, model=None):
    """Ask the LM several prompts at once; returns one reply (or "Error: ...") per prompt."""
    prompts = list(prompts)
    try:
        _r = _http.post(_PROXY + "/llm_query_batched", json={{"prompts": prompts, "model": model}}, timeout=_LLM_TIMEOUT)
        _d = _r.json()
        if _d.get("error") is not None:
            return [f"Error: {{_d['error']}}"] * len(prompts)
        _out = _d["responses"]
        if not isinstance(_out, list) or len(_out) != len(prompts):
            raise ValueError(f"expected {{len(prompts)}} responses")
        return _out
    except Exception as _e:
        return [f"Error: {{_e}}"] * len(prompts)


def _user_names():
    return [_n for _n in list(globals()) if not _n.startswith("_") and _n not in _HIDDEN]


def SHOW_VARS():
    """List the variables your code has created, with their types."""
    _vars = {{_n: type(globals()[_n]).__name__ for _n in _user_names()}}
    if not _vars:
        return "No variables created yet."
    return f"Available variables: {{_vars}}"


if not isinstance(globals().get("answer"), dict):
    answer = {{"content": "", "ready": False}}


def _cap(_text):
    if len(_text) <= _MAX_OUTPUT:
        return _text
    _half = _MAX_OUTPUT // 2
    return _text[:_half] + f"\\n[... {{len(_text) - 2 * _half}} characters cut ...]\\n" + _text[-_half:]


def _safe_repr(_value):
    try:
        _text = repr(_value)
    except Exception as _e:
        return f"<repr failed: {{type(_e).__name__}}>"
    return _text if len(_text) <= _MAX_REPR else _text[:_MAX_REPR] + "..."


def _print_error(_exc, _file):
    # Skip this script's own frame so the traceback starts in the user's code.
    _traceback.print_exception(type(_exc), _exc, _exc.__traceback__.tb_next, file=_file)


# 3. Load the custom tools, then run the code, with output captured.
_out, _err = _io.StringIO(), _io.StringIO()
_err.write("".join(_notes))
with _contextlib.redirect_stdout(_out), _contextlib.redirect_stderr(_err):
    for _kind, _name, _src in _TOOLS:
        try:
            if _kind == "code":
                exec(compile(_src, f"<tool {{_name}}>", "exec"), globals())
            elif _kind == "lambda":
                globals()[_name] = eval(compile(_src, f"<tool {{_name}}>", "eval"), globals())
            else:
                globals()[_name] = _json.loads(_src)
        except Exception as _e:
            _err.write(f"Error loading tool {{_name!r}}:\\n")
            _print_error(_e, _err)
    # Register the source so tracebacks can show the lines of "<repl>".
    _linecache.cache["<repl>"] = (len(_CODE), None, _CODE.splitlines(True), "<repl>")
    try:
        exec(compile(_CODE, "<repl>", "exec"), globals())
    except (Exception, SystemExit) as _e:  # keep going: variables set before the error stay
        _print_error(_e, _err)

# 4. Save the variables that can be pickled: temp file, then atomic rename.
_clean = {{}}
for _n in _user_names():
    try:
        _dill.dumps(globals()[_n])
        _clean[_n] = globals()[_n]
    except Exception:
        pass  # e.g. generators, open files, sockets
if isinstance(globals().get("answer"), dict):
    _clean["answer"] = answer
try:
    with open(_STATE + ".tmp", "wb") as _f:
        _dill.dump(_clean, _f)
    _os.replace(_STATE + ".tmp", _STATE)
except Exception as _e:
    _err.write(f"Warning: could not save variables ({{type(_e).__name__}}: {{_e}}).\\n")

# 5. One JSON line, after the redirect has ended. The leading newline keeps it on
#    its own line even if the code wrote to the real stdout without a newline.
_answer = globals().get("answer")
_final = str(_answer.get("content")) if isinstance(_answer, dict) and _answer.get("ready") else None
_sys.stdout.write("\\n" + _json.dumps({{
    "stdout": _cap(_out.getvalue()),
    "stderr": _cap(_err.getvalue()),
    "locals": {{_n: _safe_repr(_v) for _n, _v in _clean.items() if _n != "answer"}},
    "final_answer": _final,
}}) + "\\n")
_sys.stdout.flush()
'''


def _tool_specs(custom_tools: dict[str, Any] | None) -> list[tuple[str, str, str]]:
    """(kind, name, source) for each tool; data is JSON text, code and lambdas are Python source."""
    specs = []
    for name, entry in (custom_tools or {}).items():
        value, _ = parse_tool_entry(entry)
        kind = tool_kind(value)
        source = json.dumps(value) if kind == "data" else value.strip()
        specs.append((kind, name, source))
    return specs


def _build_exec_script(
    code: str,
    proxy_url: str,
    custom_tools: dict[str, Any] | None = None,
    workspace: str = "/workspace",
) -> str:
    """Return the source of a standalone script that runs `code` in the REPL.

    `code` is embedded base64-encoded, never pasted in, so nothing in it can
    break the script. Every other value goes in through repr().
    """
    return _TEMPLATE.format(
        proxy_url=proxy_url,
        workspace=workspace,
        llm_timeout=DEFAULT_TIMEOUT,
        max_output=MAX_OUTPUT_CHARS,
        max_repr=MAX_REPR_CHARS,
        tools=_tool_specs(custom_tools),
        code_b64=base64.b64encode(code.encode("utf-8")).decode("ascii"),
    )
