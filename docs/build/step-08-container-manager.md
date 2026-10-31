# Step 8 — `ContainerManager`

## Goal

The class that owns one Docker container and its proxy. It starts the
container, makes sure `dill` and `requests` are installed, runs code blocks
with `docker exec`, points the proxy at each round's LMHandler, starts each
round with a fresh `answer`, and cleans everything up. This is the first step
that needs Docker. It is tested in two layers:

* **Unit tests (no Docker):** `subprocess.run` is replaced with a fake that
  records the commands and returns canned results. They check the exact
  `docker` commands, result parsing, timeouts, rounds and cleanup.
* **Integration tests (`@pytest.mark.docker`):** a real container. They're
  skipped automatically where Docker isn't running.

## Tasks at a glance

1. **`REPLResult`**, the dataclass `execute_code` returns.
2. **`__init__` and `setup()`:**
   - validate the tools before any Docker command runs;
   - make a workspace folder and start the proxy;
   - `docker run` the container;
   - install `dill` and `requests` only if they're missing;
   - clean up if anything fails part-way.
3. **`execute_code`:** write `_exec.py`, run it under `timeout -k`, and turn
   its output into a `REPLResult`. Output that bypassed the script's capture
   is put in front of the `stdout` field.
4. **Rounds:** `update_handler_address` sets the address and a "new round"
   flag. The next code block passes `new_round=True` to the script, and the
   flag is cleared only once a block has saved its state.
5. **`cleanup()`:** idempotent, carries on past failures, and empties
   `/workspace` from inside the container first.
6. **Write the tests.** `tests/test_container_manager.py` has 28 unit tests
   (29 cases with parametrize) and 13 integration tests.
7. **Check that the tests catch real bugs** by making each mistake in the
   Pitfalls table on purpose.

**Outcome:** `pytest -q` shows 29 more passing test cases without Docker, or
42 with Docker. The CI table shows step 8 as ✅.

## What you'll learn

* **Driving the Docker CLI** with `subprocess.run`.
* **Container networking:** why `--add-host host.docker.internal:host-gateway`
  is needed on Linux, and why the proxy binds `0.0.0.0`.
* **Timeouts at two levels:** `timeout` inside the container (with a SIGKILL
  fallback), and the `subprocess` timeout as a backstop.
* **State that must be committed:** why the new-round flag is cleared only
  after a block saves.
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
WORKSPACE_BASE: str = ".dockerrepl_workspace"   # host folder for the workspaces
SETUP_TIMEOUT: int = 600                        # seconds per setup command (docker run may pull)
CLEANUP_TIMEOUT: int = 30
KILL_AFTER: int = 5                             # `timeout -k`: SIGKILL this long after SIGTERM
TIMEOUT_EXIT_CODES = frozenset({124, 137})      # `timeout` after SIGTERM / after SIGKILL

@dataclass
class REPLResult:
    stdout: str
    stderr: str
    locals: dict[str, str] = {}               # field(default_factory=dict)
    final_answer: str | None = None
    execution_time: float = 0.0

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
    proxy_server: ThreadingProxyServer | None
    proxy_thread: threading.Thread | None
    proxy_port: int
    def setup(self) -> None: ...
    def execute_code(self, code: str) -> REPLResult: ...
    def update_handler_address(self, address: tuple[str, int]) -> None: ...
    def cleanup(self) -> None: ...
    def __enter__(self) -> "ContainerManager": ...
    def __exit__(self, *exc: object) -> None: ...
```

`persistent` is only stored here. The Client (step 9) decides whether to keep
the container between rounds.

## Implementation guide

1. **`__init__`:**
   - Call `validate_custom_tools` **first**, so bad tools fail before any
     Docker command runs.
   - Store the arguments, with `custom_tools or {}`. Set `container_id = None`,
     `_new_round = False` and `_cleaned_up = False`.
   - Call `setup()`. If it raises, call `cleanup()` and re-raise, so a
     half-built manager leaves nothing behind.
2. **A helper `_docker(*args, timeout)`** runs
   `subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)`.
   It turns `FileNotFoundError` into
   `RuntimeError("the docker command was not found; is Docker installed?")`.
3. **`setup()`:**
   - `temp_dir = tempfile.mkdtemp(prefix="dockerrepl_", dir=<abs WORKSPACE_BASE>)`,
     creating the base folder if needed. The base is in the current directory
     because Docker Desktop on macOS and Windows can only mount folders it
     shares, and the system temp folder may not be one of them. It's in
     `.gitignore`.
   - `proxy_server, proxy_thread = start_proxy_server(self, "0.0.0.0", 0)`,
     then `proxy_port = proxy_server.server_address[1]`.
   - `docker run -d --rm -v <temp_dir>:/workspace --add-host host.docker.internal:host-gateway <image> tail -f /dev/null`.
     A non-zero exit raises `RuntimeError` with the command's stderr.
     `container_id = stdout.strip()`.
   - `docker exec <id> python -c "import dill, requests"`. **Only if that
     fails**, run `docker exec <id> pip install -q dill requests` and check
     again. A second failure raises
     `RuntimeError("could not install dill/requests in <image>: ...")`.
     Checking first means an image that already has both starts in about a
     second, and works without network access.
4. **`execute_code(code)`:**
   - Raise `RuntimeError` if there's no container or it was cleaned up.
   - Record the start time. Write
     `_build_exec_script(code, f"http://host.docker.internal:{proxy_port}", custom_tools, new_round=self._new_round)`
     to `<temp_dir>/_exec.py`.
   - Run `docker exec <id> timeout -k 5 <exec_timeout> python /workspace/_exec.py`,
     with a `subprocess` timeout of `exec_timeout + 15` as a backstop in case
     the `docker` command itself hangs.
   - **Exit code 124 or 137**, or `subprocess.TimeoutExpired`, gives
     `REPLResult(stdout="", stderr=f"Execution timed out after {exec_timeout} s")`.
     `timeout` sends SIGTERM at the limit, which ends normal code (124). Code
     that ignores SIGTERM is killed with SIGKILL five seconds later (137).
   - **Otherwise**, parse the last line of stdout as JSON:
     - The script writes a newline just before its JSON line. Everything
       before that newline was written straight to the real stdout, for
       example by a subprocess. Put it in front of the `stdout` field, ending
       in a newline.
     - Add the process's own stderr (for example a `docker` warning) to
       `stderr`. Cut both with the same limit as the script
       (`MAX_OUTPUT_CHARS`).
     - **Clear `_new_round`** now. The script ran to the end and saved, so the
       fresh `answer` is stored.
   - **If there is no JSON line**, for example because Python crashed before
     the script got going:
     `REPLResult(stdout=<raw stdout>, stderr=<raw stderr> or "Could not parse execution result")`.
     Leave `_new_round` set.
   - `execution_time` is the elapsed time in all cases.
5. **`update_handler_address(address)`:** set `self.lm_handler_address` (the
   proxy reads it on its next request) and `_new_round = True`. It runs no
   command of its own. A separate "reset `answer`" command could fail by
   itself, and the new round would then end at once with the previous
   round's answer.
6. **`cleanup()`** is idempotent: return at once if `_cleaned_up`, and set it
   first.
   - **If there is a container:**
     `docker exec <id> find /workspace -mindepth 1 -delete`, then
     `docker rm -f <id>`. Files that root creates in the container may not be
     deletable from the host, so they're removed from inside. `find` also
     removes hidden files, which `rm -rf /workspace/*` would miss. If a
     command fails or times out, carry on.
   - **If there is a proxy:** `shutdown()`, `server_close()`, then join the
     thread.
   - `shutil.rmtree(temp_dir, ignore_errors=True)`.
7. **`__enter__`** returns `self`. **`__exit__`** calls `cleanup()`.

## Tests — `tests/test_container_manager.py`

### Unit tests (no Docker)

`FakeDocker` is a callable that replaces `subprocess.run` through
`monkeypatch.setattr(container_manager.subprocess, "run", fake)`. It records
every command and answers by rules:
* `docker run` returns `stdout="abc123\n"`, or `run_result` if a test sets it.
* `python -c "import dill, requests"` succeeds, unless a test queues failures
  in `import_results`.
* `... python /workspace/_exec.py` saves a copy of the script in `scripts`
  and returns `exec_result`, or raises it if it's an exception.
* `fail_on["rm"] = exc` makes that subcommand raise.
* Everything else returns exit code 0.

The `fake` fixture also points `WORKSPACE_BASE` at `tmp_path`. The `make`
fixture builds managers and cleans them all up at the end.
`script_output(data, before="")` builds what the script prints: `before`,
a newline, the JSON line.

| Test | Checks |
|---|---|
| **Setup** | |
| `test_invalid_tools_fail_before_docker` | `custom_tools={"llm_query": 1}` raises `ValueError`, and no command ran |
| `test_docker_run_command` | `-d`, `--rm`, `-v <temp_dir>:/workspace`, `--add-host host.docker.internal:host-gateway`, then the image and `tail -f /dev/null` |
| `test_workspace_is_a_new_folder_under_the_base` | each manager gets its own folder directly under `WORKSPACE_BASE` |
| `test_skips_pip_when_already_installed` | no `pip` command when the import check passes |
| `test_installs_dependencies_when_missing` | check fails, then `pip install -q dill requests`, then the check again |
| `test_install_failure_raises_and_cleans_up` | both checks fail: `RuntimeError`, `docker rm -f`, and the workspace is gone |
| `test_setup_failure_cleans_up` | `docker run` fails: `RuntimeError`, the proxy thread stopped and its port closed, the workspace gone, no `rm` |
| `test_docker_missing_gives_clear_error` | no `docker` command: "is Docker installed?" |
| `test_proxy_started` | `proxy_port > 0`, and the proxy answers `POST /llm_query` (body `{"prompt": "hi"}`) with "No LM handler configured" |
| **`execute_code`** | |
| `test_execute_writes_script_with_base64_code` | the script holds the base64 of the code and the `host.docker.internal` proxy URL |
| `test_execute_uses_timeout_wrapper` | the command is `[..., "timeout", "-k", "5", "600", "python", "/workspace/_exec.py"]` |
| `test_execute_parses_result` | the JSON fields become the `REPLResult` fields |
| `test_execute_keeps_output_that_bypassed_the_capture` | `"from a subprocess\n"` before the JSON line goes in front of `stdout` |
| `test_execute_bypassed_output_without_newline` | `"raw"` with no newline becomes `"raw\n"` in front |
| `test_execute_appends_docker_stderr` | the process's stderr is added to `stderr` |
| `test_execute_timeout_exit_code` (124, 137) | `"Execution timed out after 600 s"`, no locals, no final answer |
| `test_execute_subprocess_timeout` | the fake raises `TimeoutExpired`: the same message, with the current `exec_timeout` |
| `test_execute_unparseable_output` | stdout `"garbage"`: kept, with "Could not parse execution result" |
| `test_execute_crash_reports_stderr` | Python's traceback reaches `stderr` |
| `test_execute_after_cleanup_raises` | `RuntimeError` once cleaned up |
| **Rounds** | |
| `test_no_new_round_before_an_address_is_set` | the first script has `_NEW_ROUND = False` |
| `test_update_handler_address_starts_a_new_round` | the address is stored; the next script has `_NEW_ROUND = True` and the one after `False`; no separate reset command ran |
| `test_new_round_kept_until_a_block_completes` | a timed-out first block leaves the flag set, so the next block resets again |
| `test_proxy_reads_manager_address` | after `update_handler_address` to a real `LMHandler(MockLM())`, the proxy answers `"Mock: hi"` |
| **Cleanup** | |
| `test_cleanup_is_idempotent` | two calls give exactly one `docker rm -f` |
| `test_cleanup_empties_workspace_inside_container_first` | the last two commands are `find /workspace -mindepth 1 -delete`, then `rm -f` |
| `test_cleanup_continues_when_docker_fails` | `rm -f` times out: the proxy is still stopped and the folder deleted |
| `test_context_manager_cleans_up` | after a `with` block: `docker rm -f abc123`, and the folder is gone |

### Integration tests (`@pytest.mark.docker`)

A module-scoped fixture, `real`, makes one `ContainerManager`, because
starting a container and installing packages takes seconds. The `env`
fixture gives each test fresh variables (it deletes `state.dill`), no handler
address, and `exec_timeout = 600`.

The image comes from the `DOCKERREPL_TEST_IMAGE` environment variable, by
default `python:3.11-slim`. If the container can't reach PyPI, for example
behind a company proxy, set it to an image that already has `dill` and
`requests`.

| Test | Checks |
|---|---|
| `test_real_container_runs_code` | `print(1 + 1)` gives `"2\n"` and no stderr |
| `test_real_state_persists` | `x = 5`, then `print(x * 2)` gives `"10\n"` |
| `test_real_llm_query_reaches_host` | with a host `LMHandler(MockLM())`, `print(llm_query("hi"))` gives `"Mock: hi\n"`. This proves `host.docker.internal` and the `0.0.0.0` binding work. |
| `test_real_llm_query_batched` | `['Mock: a', 'Mock: b']` |
| `test_real_handler_switch` | two LMHandlers with `PrefixLM("A")` and `PrefixLM("B")` (from step 7's tests): after switching, `llm_query("x")` gives `"B:x"` |
| `test_real_show_vars` | after `x = 1`, `SHOW_VARS()` mentions `'x': 'int'` |
| `test_real_new_round_resets_answer` | `ready = True` saved, then `update_handler_address`: the next block sees `ready` false, and other variables are kept |
| `test_real_final_answer` | `answer["ready"] = True` gives `final_answer == "42"` |
| `test_real_output_that_bypasses_capture` | `subprocess.run(["echo", "from echo"])` output is in `stdout`, before `print`'s |
| `test_real_timeout_keeps_state` | with `exec_timeout = 2`: `y = 1`; then `y = 2` and a loop times out; `y` is still `1` |
| `test_real_timeout_when_sigterm_is_ignored` | with `exec_timeout = 1`, code that ignores SIGTERM is still stopped, within about 1 + 5 s |
| `test_real_custom_tools` | a manager with `{"triple": ...}`: `print(triple(4))` gives `"12\n"` |
| `test_real_cleanup_removes_container_and_files` | files root made in the container (including a subfolder) are gone, and `docker inspect` fails |

## Run

```bash
pytest -q tests/test_container_manager.py              # Docker tests skip if no daemon
pytest -q -m docker tests/test_container_manager.py    # only the real-container tests
DOCKERREPL_TEST_IMAGE=my-image:tag pytest -q -m docker tests/test_container_manager.py
```

## Done when

- [ ] All unit tests pass on a machine **without** Docker.
- [ ] All integration tests pass on a machine **with** Docker.
- [ ] Steps 0–7 still pass.
- [ ] After the test run, `docker ps -a` shows no leftover containers, and
      `.dockerrepl_workspace/` is empty.
- [ ] Each mistake in the Pitfalls table, made on purpose, makes the listed
      test fail. Undo it afterwards.
- [ ] The **Tests** workflow on GitHub shows step 8 as ✅.

## Pitfalls

Each row was checked by making the mistake on purpose:

| Mistake | What goes wrong | Caught by |
|---|---|---|
| **Binding the proxy to `127.0.0.1`** | on native Linux the container reaches the host at the bridge gateway, such as `172.17.0.1`, and is refused | `test_real_llm_query_reaches_host` and two others |
| **Relying only on the `subprocess` timeout** | it kills the `docker` command on the host, but the Python process in the container keeps running | `test_execute_uses_timeout_wrapper` |
| **`timeout` without `-k`** | code that ignores SIGTERM runs forever | `test_execute_uses_timeout_wrapper` |
| **Treating only 124 as a timeout** | a SIGKILLed run (137) looks like a crash | `test_execute_timeout_exit_code[137]` |
| **Clearing the new-round flag before the block finishes** | if the first block of a round times out, the next block sees the old `ready=True`, and the round ends with the previous answer | `test_new_round_kept_until_a_block_completes` |
| **Never setting the new-round flag** | the same, every round | `test_update_handler_address_starts_a_new_round` and others |
| **Not cleaning up when `setup()` fails part-way** | a proxy thread or container is leaked on every failure | `test_setup_failure_cleans_up`, `test_install_failure_raises_and_cleans_up` |
| **Validating the tools after starting Docker** | a typo in a tool costs a container start | `test_invalid_tools_fail_before_docker` |
| **Forgetting `--rm`** | stopped containers pile up | `test_docker_run_command` |
| **Not emptying `/workspace` from inside** | root-owned files are left in the workspace folder | `test_cleanup_empties_workspace_inside_container_first` |
| **Stopping cleanup at the first failure** | the proxy and folder are leaked when `docker rm` fails | `test_cleanup_continues_when_docker_fails` |
| **A cleanup that isn't idempotent** | `docker rm` runs again on every call | `test_cleanup_is_idempotent` |
| **Dropping the lines before the JSON line** | subprocess output never reaches the LM | `test_execute_keeps_output_that_bypassed_the_capture` and two others |
| **Not adding the process's stderr** | `docker` errors are invisible | `test_execute_appends_docker_stderr` |
| **Always running `pip install`** | every container needs the network and takes longer | `test_skips_pip_when_already_installed` |
| **Letting `FileNotFoundError` escape** | "No such file or directory: 'docker'" instead of a clear message | `test_docker_missing_gives_clear_error` |

Removing the `timeout` wrapper is only caught by the unit test. The real
timeout test still passes, more slowly, because the `subprocess` backstop
fires instead, but the Python process keeps running in the container.

**A function-scoped fixture for the integration tests** would also work, but
every test would then pay for a container start, and for `pip install` when
the image doesn't have the packages.
