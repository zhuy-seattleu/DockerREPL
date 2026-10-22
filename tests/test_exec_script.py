"""Step 6: the generated _exec.py script, run locally as a subprocess."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from dockerrepl.environments.exec_script import MAX_OUTPUT_CHARS, _build_exec_script

CLOSED_PROXY = "http://127.0.0.1:9"  # port 9 ("discard") is almost always closed


def run_script(
    tmp_path: Path, code: str, proxy_url: str = CLOSED_PROXY, custom_tools: dict | None = None
) -> dict[str, Any]:
    """Build the script for `code`, run it with `tmp_path` as the workspace, return its JSON line."""
    script = _build_exec_script(code, proxy_url, custom_tools, workspace=str(tmp_path))
    path = tmp_path / "_exec.py"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(path)], capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


class FakeProxy:
    """A tiny HTTP server standing in for LMProxyHandler (step 7). Records each request."""

    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.requests: list[tuple[str, dict[str, Any]]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append((self.path, body))
                data = json.dumps(fake.reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_proxy() -> Iterator[FakeProxy]:
    proxy = FakeProxy({"response": "pong"})
    yield proxy
    proxy.close()


# ---------------------------------------------------------------- output


def test_prints_stdout(tmp_path: Path) -> None:
    assert run_script(tmp_path, 'print("hi")')["stdout"] == "hi\n"


def test_unicode_output(tmp_path: Path) -> None:
    assert run_script(tmp_path, 'print("héllo ✓")')["stdout"] == "héllo ✓\n"


def test_stderr_captured(tmp_path: Path) -> None:
    result = run_script(tmp_path, 'import sys\nprint("warn", file=sys.stderr)')
    assert result["stderr"] == "warn\n" and result["stdout"] == ""


def test_exception_goes_to_stderr(tmp_path: Path) -> None:
    result = run_script(tmp_path, "1/0")
    assert "ZeroDivisionError" in result["stderr"]
    assert result["final_answer"] is None


def test_traceback_points_at_user_code(tmp_path: Path) -> None:
    stderr = run_script(tmp_path, "x = 1\ny = x / 0")["stderr"]
    assert 'File "<repl>", line 2' in stderr
    assert "y = x / 0" in stderr  # the source line is shown
    assert "_exec.py" not in stderr  # the script's own frame is hidden


def test_syntax_error_reported(tmp_path: Path) -> None:
    stderr = run_script(tmp_path, "def f(:\n    pass")["stderr"]
    assert "SyntaxError" in stderr and "<repl>" in stderr


def test_sys_exit_still_reports(tmp_path: Path) -> None:
    result = run_script(tmp_path, 'print("before")\nimport sys\nsys.exit(3)')
    assert result["stdout"] == "before\n"
    assert "SystemExit" in result["stderr"]


def test_raw_write_without_newline(tmp_path: Path) -> None:
    # Bypasses sys.stdout; the result line must still be a line of its own.
    result = run_script(tmp_path, 'import os\nos.write(1, b"raw")\nprint("ok")')
    assert result["stdout"] == "ok\n"


def test_long_output_cut(tmp_path: Path) -> None:
    stdout = run_script(tmp_path, 'print("a" * 100_000 + "END")')["stdout"]
    assert len(stdout) < MAX_OUTPUT_CHARS + 100
    assert "characters cut" in stdout
    assert stdout.startswith("aaa") and stdout.endswith("END\n")


# ---------------------------------------------------------------- state


def test_variables_persist_between_runs(tmp_path: Path) -> None:
    run_script(tmp_path, "x = 41")
    assert run_script(tmp_path, "print(x + 1)")["stdout"] == "42\n"


def test_partial_state_kept_after_error(tmp_path: Path) -> None:
    run_script(tmp_path, "a = 1\n1/0\nb = 2")
    result = run_script(tmp_path, "print(a)\nprint(b)")
    assert result["stdout"] == "1\n"
    assert "NameError" in result["stderr"] and "'b'" in result["stderr"]


def test_unpicklable_value_skipped(tmp_path: Path) -> None:
    run_script(tmp_path, "gen = (i for i in range(3))\ny = 2")
    result = run_script(tmp_path, "print(y)\nprint(gen)")
    assert result["stdout"] == "2\n"
    assert "NameError" in result["stderr"] and "gen" in result["stderr"]


def test_function_sees_current_variables(tmp_path: Path) -> None:
    # A function saved in run 1 must read today's x, not a frozen copy.
    run_script(tmp_path, "x = 1\ndef f():\n    return x")
    assert run_script(tmp_path, "x = 5\nprint(f())")["stdout"] == "5\n"


def test_function_kept_next_to_unpicklable_value(tmp_path: Path) -> None:
    run_script(tmp_path, "def f():\n    return 7\ngen = (i for i in range(3))")
    assert run_script(tmp_path, "print(f())")["stdout"] == "7\n"


def test_saved_function_can_call_helpers(tmp_path: Path) -> None:
    run_script(tmp_path, 'def ask():\n    return llm_query("hi")')
    assert run_script(tmp_path, "print(ask())")["stdout"].startswith("Error:")


def test_class_and_instance_persist(tmp_path: Path) -> None:
    run_script(tmp_path, "class P:\n    def __init__(self):\n        self.v = 3\np = P()")
    assert run_script(tmp_path, "print(p.v, P().v)")["stdout"] == "3 3\n"


def test_import_persists(tmp_path: Path) -> None:
    run_script(tmp_path, "import math")
    assert run_script(tmp_path, "print(math.sqrt(16))")["stdout"] == "4.0\n"


def test_state_written_atomically(tmp_path: Path) -> None:
    run_script(tmp_path, "x = 1")
    assert (tmp_path / "state.dill").exists()
    assert not (tmp_path / "state.dill.tmp").exists()


def test_failed_save_keeps_old_state(tmp_path: Path) -> None:
    # Flaky passes the "can it be pickled?" check, then fails during the real
    # save, halfway through writing the file. The previous state must survive.
    run_script(tmp_path, "x = 1")
    flaky = (
        "class Flaky:\n"
        "    calls = 0\n"
        "    def __reduce__(self):\n"
        "        Flaky.calls += 1\n"
        "        if Flaky.calls > 1:\n"
        "            raise RuntimeError('fails on the real save')\n"
        "        return (int, (0,))\n"
        "x = 2\n"
        "f = Flaky()"
    )
    assert "could not save variables" in run_script(tmp_path, flaky)["stderr"]
    assert run_script(tmp_path, "print(x)")["stdout"] == "1\n"


def test_corrupt_state_starts_fresh(tmp_path: Path) -> None:
    (tmp_path / "state.dill").write_bytes(b"not a pickle")
    result = run_script(tmp_path, "print(SHOW_VARS())")
    assert result["stdout"] == "No variables created yet.\n"
    assert "could not load saved variables" in result["stderr"]


# ---------------------------------------------------------------- locals and SHOW_VARS


def test_locals_reports_repr(tmp_path: Path) -> None:
    assert run_script(tmp_path, "z = [1, 2]")["locals"]["z"] == "[1, 2]"


def test_long_repr_cut(tmp_path: Path) -> None:
    text = run_script(tmp_path, "big = list(range(100_000))")["locals"]["big"]
    assert len(text) <= 503 and text.endswith("...")


def test_bad_repr_doesnt_crash(tmp_path: Path) -> None:
    code = "class B:\n    def __repr__(self):\n        raise RuntimeError\nb = B()"
    assert run_script(tmp_path, code)["locals"]["b"] == "<repr failed: RuntimeError>"


def test_locals_hide_helpers_tools_and_internals(tmp_path: Path) -> None:
    names = set(run_script(tmp_path, "x = 1", custom_tools={"CONST": 7})["locals"])
    assert names == {"x"}


def test_show_vars_lists_user_variables(tmp_path: Path) -> None:
    out = run_script(tmp_path, 'x = 1\ns = "a"\nprint(SHOW_VARS())')["stdout"]
    assert "'x': 'int'" in out and "'s': 'str'" in out
    assert "llm_query" not in out and "answer" not in out and "_" not in out.replace("Available", "")


def test_show_vars_when_empty(tmp_path: Path) -> None:
    assert run_script(tmp_path, "print(SHOW_VARS())")["stdout"] == "No variables created yet.\n"


# ---------------------------------------------------------------- answer


def test_final_answer(tmp_path: Path) -> None:
    result = run_script(tmp_path, 'answer["content"] = "done"\nanswer["ready"] = True')
    assert result["final_answer"] == "done"


def test_no_final_answer_by_default(tmp_path: Path) -> None:
    assert run_script(tmp_path, "x = 1")["final_answer"] is None


def test_answer_survives_between_runs(tmp_path: Path) -> None:
    run_script(tmp_path, 'answer["content"] = "partial"')
    assert run_script(tmp_path, 'print(answer["content"])')["stdout"] == "partial\n"


def test_answer_replaced_when_not_a_dict(tmp_path: Path) -> None:
    assert run_script(tmp_path, "answer = 5")["final_answer"] is None
    out = run_script(tmp_path, "print(answer)")["stdout"]
    assert out == "{'content': '', 'ready': False}\n"


# ---------------------------------------------------------------- embedding


def test_tricky_code_is_safe(tmp_path: Path) -> None:
    code = "s = '''a\"\"\"b'''\nprint(s, '\\\\', '{}', \"{x}\")"
    assert run_script(tmp_path, code)["stdout"] == 'a"""b \\ {} {x}\n'


def test_code_is_never_pasted_in(tmp_path: Path) -> None:
    script = _build_exec_script('print("UNIQUE_MARKER_42")', CLOSED_PROXY)
    assert "UNIQUE_MARKER_42" not in script


# ---------------------------------------------------------------- custom tools


def test_custom_code_tool_available(tmp_path: Path) -> None:
    tools = {"triple": "def triple(x):\n    return x * 3"}
    assert run_script(tmp_path, "print(triple(5))", custom_tools=tools)["stdout"] == "15\n"


def test_custom_lambda_tool_available(tmp_path: Path) -> None:
    tools = {"double": "lambda x: 2 * x"}
    assert run_script(tmp_path, "print(double(4))", custom_tools=tools)["stdout"] == "8\n"


def test_custom_data_tool_available(tmp_path: Path) -> None:
    tools = {"CONST": 7, "LOOKUP": {"tool": {"a": 1}, "description": "d"}, "NAME": "x = 5"}
    out = run_script(tmp_path, 'print(CONST, LOOKUP["a"], NAME)', custom_tools=tools)["stdout"]
    assert out == "7 1 x = 5\n"


def test_tools_reinjected_after_overwrite(tmp_path: Path) -> None:
    run_script(tmp_path, "CONST = 0", custom_tools={"CONST": 7})
    assert run_script(tmp_path, "print(CONST)", custom_tools={"CONST": 7})["stdout"] == "7\n"


def test_helpers_reinjected_after_overwrite(tmp_path: Path) -> None:
    run_script(tmp_path, "llm_query = None")
    assert run_script(tmp_path, "print(callable(llm_query))")["stdout"] == "True\n"


def test_broken_tool_reported_and_code_still_runs(tmp_path: Path) -> None:
    tools = {"f": "import not_a_real_module\ndef f():\n    pass"}
    result = run_script(tmp_path, 'print("ran")', custom_tools=tools)
    assert result["stdout"] == "ran\n"
    assert "Error loading tool 'f'" in result["stderr"]
    assert "ModuleNotFoundError" in result["stderr"]


# ---------------------------------------------------------------- llm_query


def test_llm_query_unreachable_returns_error_string(tmp_path: Path) -> None:
    result = run_script(tmp_path, 'print(llm_query("x"))')
    assert result["stdout"].startswith("Error:")
    assert result["stderr"] == ""


def test_llm_query_batched_unreachable(tmp_path: Path) -> None:
    out = run_script(tmp_path, 'r = llm_query_batched(["a", "b"])\nprint(len(r), all(s.startswith("Error:") for s in r))')
    assert out["stdout"] == "2 True\n"


def test_llm_query_posts_to_proxy(tmp_path: Path, fake_proxy: FakeProxy) -> None:
    result = run_script(tmp_path, 'print(llm_query("hi"))', proxy_url=fake_proxy.url)
    assert result["stdout"] == "pong\n"
    assert fake_proxy.requests == [("/llm_query", {"prompt": "hi", "model": None})]


def test_llm_query_error_reply(tmp_path: Path, fake_proxy: FakeProxy) -> None:
    fake_proxy.reply = {"error": "boom"}
    assert run_script(tmp_path, 'print(llm_query("hi"))', proxy_url=fake_proxy.url)["stdout"] == "Error: boom\n"


def test_llm_query_batched_posts_to_proxy(tmp_path: Path, fake_proxy: FakeProxy) -> None:
    fake_proxy.reply = {"responses": ["A", "Error: bad"]}
    code = 'print(llm_query_batched(["a", "b"], model="m"))'
    assert run_script(tmp_path, code, proxy_url=fake_proxy.url)["stdout"] == "['A', 'Error: bad']\n"
    assert fake_proxy.requests == [("/llm_query_batched", {"prompts": ["a", "b"], "model": "m"})]


def test_llm_query_batched_wrong_count(tmp_path: Path, fake_proxy: FakeProxy) -> None:
    fake_proxy.reply = {"responses": ["only one"]}
    code = 'r = llm_query_batched(["a", "b"])\nprint(len(r), all(s.startswith("Error:") for s in r))'
    assert run_script(tmp_path, code, proxy_url=fake_proxy.url)["stdout"] == "2 True\n"


def test_llm_query_ignores_proxy_environment(tmp_path: Path, fake_proxy: FakeProxy, monkeypatch: pytest.MonkeyPatch) -> None:
    # A proxy variable from the environment must not capture the call to the LM proxy.
    for var in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    assert run_script(tmp_path, 'print(llm_query("hi"))', proxy_url=fake_proxy.url)["stdout"] == "pong\n"
