# Step 8 — `ContainerManager`

## Goal

The class that owns one Docker container and its proxy. It starts the
container, installs `dill` and `requests`, runs code blocks with
`docker exec`, points the proxy at each round's LMHandler, and cleans
everything up. This is the first step that needs Docker. It is tested in two
layers:

* **Unit tests (no Docker):** `subprocess.run` is replaced with a fake that
  records the commands and returns canned results. They check the exact
  `docker` commands, result parsing, timeouts and cleanup.
* **Integration tests (`@pytest.mark.docker`):** a real container. They're
  skipped automatically where Docker isn't running.

## What you'll learn

* **Driving the Docker CLI** with `subprocess.run`.
* **Container networking:** why `--add-host host.docker.internal:host-gateway`
  is needed on Linux, and why the proxy binds `0.0.0.0`.
* **Timeouts at two levels:** `timeout` inside the container, and the
  `subprocess` timeout as a backstop.
* **Idempotent cleanup** and context managers.
* **Test doubles:** `monkeypatch` and a fake `subprocess.run`.

## Depends on

Steps 5, 6 and 7. The integration tests also use steps 2 and 4.

## Files

| File | Purpose |
|---|---|
| `dockerrepl/environments/container_manager.py` | `REPLResult`, `ContainerManager` |
| `tests/test_container_manager.py` | tests below |

## Interface

```python
@dataclass
class REPLResult:
    stdout: str
    stderr: str
    locals: dict[str, str]
    final_answer: str | None
    execution_time: float

class ContainerManager:
    def __init__(self, image: str = "python:3.11-slim",
                 lm_handler_address: tuple[str, int] | None = None,
                 persistent: bool = False,
                 custom_tools: dict[str, Any] | None = None,
                 exec_timeout: int = 600) -> None: ...
    image: str
    persistent: bool
    custom_tools: dict[str, Any]
    exec_timeout: int
    lm_handler_address: tuple[str, int] | None
    container_id: str | None
    temp_dir: str
    proxy_server: ThreadingHTTPServer | None
    proxy_thread: threading.Thread | None
    proxy_port: int
    def setup(self) -> None: ...
    def execute_code(self, code: str) -> REPLResult: ...
    def update_handler_address(self, address: tuple[str, int]) -> None: ...
    def cleanup(self) -> None: ...
    def __enter__(self) -> "ContainerManager": ...
    def __exit__(self, *exc: object) -> None: ...
```

## Implementation guide

1. **`__init__`:**
   - Store the arguments, with `custom_tools or {}`.
   - Call `validate_custom_tools` **first**, so bad tools fail before any
     Docker command runs.
   - Set `container_id = None` and `_cleaned_up = False`.
   - Call `setup()`. If `setup()` raises, call `cleanup()` and re-raise, so a
     half-built manager leaves nothing behind.
2. **`setup()`:**
   - `temp_dir = tempfile.mkdtemp(prefix="dockerrepl_", dir=<base>)`.
     `<base>` is `.dockerrepl_workspace/` in the current directory, created if
     needed. Docker Desktop on macOS can only mount folders it shares, and the
     system temp folder may not be one of them.
   - `proxy_server, proxy_thread = start_proxy_server(self, "0.0.0.0", 0)`,
     then `proxy_port = proxy_server.server_address[1]`.
   - `docker run -d --rm -v <temp_dir>:/workspace --add-host host.docker.internal:host-gateway <image> tail -f /dev/null`.
     A non-zero exit raises `RuntimeError` with the command's stderr.
   - `container_id = stdout.strip()`.
   - `docker exec <id> pip install -q dill requests`.
   - Then `docker exec <id> python -c "import dill, requests"`. A non-zero exit
     raises `RuntimeError("could not install dill/requests in <image>: ...")`.
3. **`execute_code(code)`:**
   - Record the start time.
   - Write `_build_exec_script(code, f"http://host.docker.internal:{proxy_port}", custom_tools)`
     to `<temp_dir>/_exec.py`.
   - Run `["docker", "exec", <id>, "timeout", str(exec_timeout), "python", "/workspace/_exec.py"]`
     with `capture_output=True, text=True, timeout=exec_timeout + 10`.
   - **Exit code 124**, or `subprocess.TimeoutExpired`, gives
     `REPLResult(stdout="", stderr=f"Execution timed out after {exec_timeout} s", locals={}, final_answer=None, ...)`.
   - **Otherwise** parse the last non-empty stdout line as JSON into a
     `REPLResult`, appending the process's own stderr (Python crashes,
     `docker` errors) to the `stderr` field.
   - **If parsing fails:** `REPLResult(stdout=raw stdout, stderr=raw stderr or "Could not parse execution result", locals={}, final_answer=None, ...)`.
   - `execution_time` is the elapsed time in all cases.
4. **`update_handler_address(address)`:**
   - `self.lm_handler_address = address`. The proxy sees it on its next
     request.
   - Then
     `self.execute_code('answer = {"content": "", "ready": False}')`.
5. **`cleanup()`** is idempotent: return at once if `_cleaned_up`.
   - **If there is a container:** `docker exec <id> sh -c "rm -rf /workspace/*"`
     (files written by root inside the container can't always be deleted from
     the host), then `docker rm -f <id>`.
   - **If there is a proxy:** `shutdown()`, `server_close()`, then join the
     thread.
   - `shutil.rmtree(temp_dir, ignore_errors=True)`.
6. **`__enter__`** returns `self`. **`__exit__`** calls `cleanup()`.

## Tests — `tests/test_container_manager.py`

### Unit tests (no Docker)

A `FakeDocker` helper is a callable that replaces `subprocess.run` through
`monkeypatch.setattr(container_manager.subprocess, "run", fake)`. It records
every argument list and returns a `subprocess.CompletedProcess` chosen by
rules:
* `docker run` returns `stdout="abc123\n"`.
* `docker exec ... python /workspace/_exec.py` returns a preset result, set by
  each test.
* Everything else returns exit code 0.

| Test | Checks |
|---|---|
| `test_invalid_tools_fail_before_docker` | `ContainerManager(custom_tools={"llm_query": 1})` raises `ValueError`, and `FakeDocker` recorded no calls |
| `test_docker_run_command` | the `run` call contains `-d`, `--rm`, `-v <temp_dir>:/workspace`, `--add-host host.docker.internal:host-gateway`, the image, and `tail -f /dev/null` |
| `test_installs_dependencies` | a `pip install -q dill requests` exec call follows `run` |
| `test_setup_failure_cleans_up` | `docker run` returns exit code 1: raises `RuntimeError`, the proxy is shut down and `temp_dir` is gone |
| `test_proxy_started` | `proxy_port > 0`, and the proxy answers `POST /llm_query` with `"No LM handler configured"` |
| `test_execute_writes_script_with_base64_code` | after `execute_code("print('x')")`, `<temp_dir>/_exec.py` exists and contains `base64.b64encode(b"print('x')").decode()` |
| `test_execute_uses_timeout_wrapper` | the exec command is `[..., "timeout", "600", "python", "/workspace/_exec.py"]` |
| `test_execute_parses_result` | a preset stdout of `"noise\n{json}\n"` gives the matching `REPLResult` fields |
| `test_execute_timeout_exit_124` | exit code 124 gives stderr `"Execution timed out after 600 s"` and `final_answer is None` |
| `test_execute_subprocess_timeout` | the fake raises `TimeoutExpired`: same as above |
| `test_execute_unparseable_output` | stdout `"garbage"` gives a `REPLResult` whose stderr says it couldn't parse the result |
| `test_update_handler_address_sets_and_resets_answer` | the attribute is updated, and the next exec script contains `answer = {"content": "", "ready": False}` |
| `test_proxy_reads_manager_address` | point `update_handler_address` at a real `LMHandler(MockLM())`; `POST /llm_query` to the proxy returns `"Mock: ..."` |
| `test_cleanup_is_idempotent` | two `cleanup()` calls give exactly one `docker rm -f` |
| `test_context_manager_cleans_up` | after a `with` block, `docker rm -f abc123` was called and `temp_dir` is gone |

### Integration tests (`@pytest.mark.docker`)

Use a module-scoped fixture for one real `ContainerManager`, because starting
a container and running `pip install` takes several seconds. Give each test
fresh variables.

| Test | Checks |
|---|---|
| `test_real_container_runs_code` | `print(1 + 1)` gives `"2\n"` |
| `test_real_state_persists` | `x = 5`, then `print(x * 2)` gives `"10\n"` |
| `test_real_llm_query_reaches_host` | with a host `LMHandler(MockLM())` and `update_handler_address`, `print(llm_query("hi"))` gives `"Mock: hi\n"`. This proves `host.docker.internal` and the `0.0.0.0` binding work. |
| `test_real_llm_query_batched` | `print(llm_query_batched(["a", "b"]))` gives `['Mock: a', 'Mock: b']` |
| `test_real_show_vars` | after `x = 1`, `SHOW_VARS()` mentions `x` |
| `test_real_custom_tools` | a manager with `{"triple": "def triple(x):\n    return x * 3"}`: `print(triple(4))` gives `"12\n"` |
| `test_real_timeout_keeps_state` | a manager with `exec_timeout=2`: `y = 1`; `while True: pass` times out; then `print(y)` gives `"1\n"` |
| `test_real_handler_switch` | two LMHandlers with `PrefixLM("A")` and `PrefixLM("B")` (from step 7's tests); after `update_handler_address` to the second, `llm_query("x")` gives `"B:x"` |
| `test_real_cleanup_removes_container` | after `cleanup()`, `docker inspect <id>` fails |

## Run

```bash
pytest -q tests/test_container_manager.py              # unit tests; Docker tests skip if no daemon
pytest -q -m docker tests/test_container_manager.py    # only the real-container tests
```

## Done when

- [ ] All unit tests pass on a machine **without** Docker.
- [ ] All integration tests pass on a machine **with** Docker.
- [ ] Steps 0–7 still pass.
- [ ] After the test run, `docker ps -a` shows no leftover containers, and
      `.dockerrepl_workspace/` is empty.

## Pitfalls

* **Binding the proxy to `127.0.0.1`.** On native Linux the container reaches
  the host through the bridge gateway, for example `172.17.0.1`, and the
  connection is refused. `test_real_llm_query_reaches_host` catches it.
* **Relying only on the `subprocess` timeout.** It kills the `docker` command
  on the host, but the Python process in the container keeps running. Use
  `timeout` inside the container as well.
* **Not cleaning up when `setup()` fails half-way.** A proxy thread or
  container is leaked on every failed test.
* **Forgetting `--rm`.** Stopped containers then pile up across test runs.
* **A function-scoped fixture for integration tests.** Every test then pays
  several seconds for `pip install`.
