"""Step 8: ContainerManager. Unit tests with a fake `docker`, then tests with a real container."""

import base64
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
import requests

from dockerrepl.clients import MockLM
from dockerrepl.environments import container_manager
from dockerrepl.environments.container_manager import ContainerManager, REPLResult
from dockerrepl.lm_handler import LMHandler
from test_proxy import PrefixLM  # step 7's test LM: answers f"{prefix}:{prompt}"

# Integration tests use this image. Point it at an image that already has dill
# and requests when the container can't reach PyPI.
TEST_IMAGE = os.environ.get("DOCKERREPL_TEST_IMAGE", "python:3.11-slim")

RESULT = {"stdout": "hi\n", "stderr": "", "locals": {"x": "1"}, "final_answer": None}


def done(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def script_output(data: dict[str, Any] = RESULT, before: str = "") -> str:
    """What _exec.py prints: anything written to the real stdout, a newline, the JSON line."""
    return before + "\n" + json.dumps(data) + "\n"


class FakeDocker:
    """Stands in for subprocess.run. Records every docker command and answers by rules."""

    def __init__(self) -> None:
        self.manager_dir = ""  # the manager's temp_dir, to read _exec.py from
        self.calls: list[list[str]] = []
        self.run_result = done(stdout="abc123\n")
        self.import_results: list[subprocess.CompletedProcess[str]] = []  # used in order; then success
        self.exec_result: subprocess.CompletedProcess[str] | Exception = done(stdout=script_output())
        self.fail_on: dict[str, Exception] = {}  # docker subcommand -> exception to raise
        self.scripts: list[str] = []  # the _exec.py contents at each run

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[0] == "docker"
        self.calls.append(list(args))
        if args[1] in self.fail_on:
            raise self.fail_on[args[1]]
        if args[1] == "run":
            return self.run_result
        if args[-1] == "/workspace/_exec.py":
            self.scripts.append(Path(self.manager_dir, "_exec.py").read_text(encoding="utf-8"))
            if isinstance(self.exec_result, Exception):
                raise self.exec_result
            return self.exec_result
        if args[-2:] == ["-c", "import dill, requests"]:
            return self.import_results.pop(0) if self.import_results else done()
        return done()

    def commands(self, kind: str) -> list[list[str]]:
        return [c for c in self.calls if c[1] == kind]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeDocker:
    fake = FakeDocker()
    monkeypatch.setattr(container_manager.subprocess, "run", fake)
    monkeypatch.setattr(container_manager, "WORKSPACE_BASE", str(tmp_path / "ws"))
    return fake


@pytest.fixture
def make(fake: FakeDocker) -> Iterator[Callable[..., ContainerManager]]:
    """Build managers on the fake docker; clean all of them up afterwards."""
    made: list[ContainerManager] = []

    def build(**kwargs: Any) -> ContainerManager:
        manager = ContainerManager(**kwargs)
        fake.manager_dir = manager.temp_dir
        made.append(manager)
        return manager

    yield build
    for manager in made:
        manager.cleanup()


# ================================================================ unit tests: setup


def test_invalid_tools_fail_before_docker(fake: FakeDocker) -> None:
    with pytest.raises(ValueError, match="llm_query"):
        ContainerManager(custom_tools={"llm_query": 1})
    assert fake.calls == []


def test_docker_run_command(make, fake: FakeDocker) -> None:
    m = make(image="img:1")
    (run,) = fake.commands("run")
    assert run[:4] == ["docker", "run", "-d", "--rm"]
    assert run[run.index("-v") + 1] == f"{m.temp_dir}:/workspace"
    assert run[run.index("--add-host") + 1] == "host.docker.internal:host-gateway"
    assert run[-4:] == ["img:1", "tail", "-f", "/dev/null"]
    assert m.container_id == "abc123"


def test_workspace_is_a_new_folder_under_the_base(make, fake: FakeDocker) -> None:
    m1, m2 = make(), make()
    base = os.path.abspath(container_manager.WORKSPACE_BASE)
    assert Path(m1.temp_dir).parent == Path(base) and os.path.isdir(m1.temp_dir)
    assert m1.temp_dir != m2.temp_dir


def test_skips_pip_when_already_installed(make, fake: FakeDocker) -> None:
    make()
    assert not any("pip" in c for c in fake.calls)


def test_installs_dependencies_when_missing(make, fake: FakeDocker) -> None:
    fake.import_results = [done(returncode=1)]  # first check fails, the second passes
    make()
    execs = fake.commands("exec")
    assert execs[1] == ["docker", "exec", "abc123", "pip", "install", "-q", "dill", "requests"]
    assert execs[2][-2:] == ["-c", "import dill, requests"]


def test_install_failure_raises_and_cleans_up(fake: FakeDocker) -> None:
    fake.import_results = [done(returncode=1), done(returncode=1, stderr="No module named dill")]
    with pytest.raises(RuntimeError, match="could not install dill/requests"):
        ContainerManager()
    assert ["docker", "rm", "-f", "abc123"] in fake.calls
    assert os.listdir(container_manager.WORKSPACE_BASE) == []


def test_setup_failure_cleans_up(fake: FakeDocker, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.run_result = done(returncode=1, stderr="no such image")
    servers = []
    real_start = container_manager.start_proxy_server
    monkeypatch.setattr(
        container_manager, "start_proxy_server", lambda *a: servers.append(real_start(*a)) or servers[-1]
    )
    with pytest.raises(RuntimeError, match="no such image"):
        ContainerManager()
    (server, thread), = servers
    assert not thread.is_alive()  # the proxy was shut down
    assert server.socket.fileno() == -1  # and its port closed
    assert os.listdir(container_manager.WORKSPACE_BASE) == []
    assert fake.commands("rm") == []  # there was no container to remove


def test_docker_missing_gives_clear_error(fake: FakeDocker) -> None:
    fake.fail_on["run"] = FileNotFoundError("docker")
    with pytest.raises(RuntimeError, match="is Docker installed"):
        ContainerManager()


def test_proxy_started(make) -> None:
    m = make()
    assert m.proxy_port > 0
    r = requests.post(f"http://127.0.0.1:{m.proxy_port}/llm_query", json={"prompt": "hi"}, timeout=5)
    assert r.json() == {"error": "No LM handler configured"}


# ================================================================ unit tests: execute_code


def test_execute_writes_script_with_base64_code(make, fake: FakeDocker) -> None:
    m = make()
    m.execute_code("print('x')")
    script = fake.scripts[-1]
    assert base64.b64encode(b"print('x')").decode() in script
    assert f"http://host.docker.internal:{m.proxy_port}" in script


def test_execute_uses_timeout_wrapper(make, fake: FakeDocker) -> None:
    make().execute_code("x = 1")
    command = fake.commands("exec")[-1]
    assert command == ["docker", "exec", "abc123", "timeout", "-k", "5", "600", "python", "/workspace/_exec.py"]


def test_execute_parses_result(make, fake: FakeDocker) -> None:
    data = {"stdout": "hi\n", "stderr": "warn\n", "locals": {"x": "1"}, "final_answer": "done"}
    fake.exec_result = done(stdout=script_output(data))
    result = make().execute_code("...")
    assert isinstance(result, REPLResult)
    assert (result.stdout, result.stderr, result.locals, result.final_answer) == ("hi\n", "warn\n", {"x": "1"}, "done")
    assert result.execution_time >= 0


def test_execute_keeps_output_that_bypassed_the_capture(make, fake: FakeDocker) -> None:
    fake.exec_result = done(stdout=script_output(before="from a subprocess\n"))
    assert make().execute_code("...").stdout == "from a subprocess\nhi\n"


def test_execute_bypassed_output_without_newline(make, fake: FakeDocker) -> None:
    fake.exec_result = done(stdout=script_output(before="raw"))
    assert make().execute_code("...").stdout == "raw\nhi\n"


def test_execute_appends_docker_stderr(make, fake: FakeDocker) -> None:
    fake.exec_result = done(stdout=script_output(), stderr="docker: a warning\n")
    assert make().execute_code("...").stderr == "docker: a warning\n"


@pytest.mark.parametrize("code", [124, 137])
def test_execute_timeout_exit_code(make, fake: FakeDocker, code: int) -> None:
    fake.exec_result = done(returncode=code)
    result = make().execute_code("while True: pass")
    assert result.stderr == "Execution timed out after 600 s"
    assert result.final_answer is None and result.locals == {}


def test_execute_subprocess_timeout(make, fake: FakeDocker) -> None:
    fake.exec_result = subprocess.TimeoutExpired(["docker"], 615)
    m = make()
    m.exec_timeout = 3
    assert m.execute_code("while True: pass").stderr == "Execution timed out after 3 s"


def test_execute_unparseable_output(make, fake: FakeDocker) -> None:
    fake.exec_result = done(stdout="garbage")
    result = make().execute_code("...")
    assert result.stdout == "garbage"
    assert result.stderr == "Could not parse execution result"


def test_execute_crash_reports_stderr(make, fake: FakeDocker) -> None:
    fake.exec_result = done(returncode=1, stderr="Traceback ...\nModuleNotFoundError: No module named 'dill'\n")
    assert "No module named 'dill'" in make().execute_code("...").stderr


def test_execute_after_cleanup_raises(make) -> None:
    m = make()
    m.cleanup()
    with pytest.raises(RuntimeError, match="not running"):
        m.execute_code("x = 1")


# ================================================================ unit tests: rounds


def test_no_new_round_before_an_address_is_set(make, fake: FakeDocker) -> None:
    make().execute_code("x = 1")
    assert "_NEW_ROUND = False" in fake.scripts[-1]


def test_update_handler_address_starts_a_new_round(make, fake: FakeDocker) -> None:
    m = make()
    m.update_handler_address(("127.0.0.1", 1234))
    assert m.lm_handler_address == ("127.0.0.1", 1234)
    m.execute_code("a = 1")
    m.execute_code("b = 2")
    assert "_NEW_ROUND = True" in fake.scripts[0]  # the first block of the round resets answer
    assert "_NEW_ROUND = False" in fake.scripts[1]  # later blocks build on it
    assert not any(c[-1].startswith("answer") for c in fake.calls)  # no separate reset command


def test_new_round_kept_until_a_block_completes(make, fake: FakeDocker) -> None:
    # A block that times out never saves, so the fresh answer isn't stored yet.
    m = make()
    m.update_handler_address(("127.0.0.1", 1234))
    fake.exec_result = done(returncode=124)
    m.execute_code("while True: pass")
    fake.exec_result = done(stdout=script_output())
    m.execute_code("a = 1")
    m.execute_code("b = 2")
    assert ["_NEW_ROUND = True" in s for s in fake.scripts] == [True, True, False]


def test_proxy_reads_manager_address(make) -> None:
    m = make()
    with LMHandler(MockLM()) as handler:
        m.update_handler_address(handler.address)
        r = requests.post(f"http://127.0.0.1:{m.proxy_port}/llm_query", json={"prompt": "hi"}, timeout=5)
        assert r.json() == {"response": "Mock: hi"}


# ================================================================ unit tests: cleanup


def test_cleanup_is_idempotent(make, fake: FakeDocker) -> None:
    m = make()
    m.cleanup()
    m.cleanup()
    assert fake.commands("rm") == [["docker", "rm", "-f", "abc123"]]


def test_cleanup_empties_workspace_inside_container_first(make, fake: FakeDocker) -> None:
    make().cleanup()
    last_two = fake.calls[-2:]
    assert last_two == [
        ["docker", "exec", "abc123", "find", "/workspace", "-mindepth", "1", "-delete"],
        ["docker", "rm", "-f", "abc123"],
    ]


def test_cleanup_continues_when_docker_fails(make, fake: FakeDocker) -> None:
    m = make()
    fake.fail_on["rm"] = subprocess.TimeoutExpired(["docker"], 30)
    m.cleanup()
    assert not m.proxy_thread.is_alive()
    assert not os.path.exists(m.temp_dir)


def test_context_manager_cleans_up(fake: FakeDocker) -> None:
    with ContainerManager() as m:
        temp_dir = m.temp_dir
        assert os.path.isdir(temp_dir)
    assert ["docker", "rm", "-f", "abc123"] in fake.calls
    assert not os.path.exists(temp_dir)


# ================================================================ integration tests (real Docker)


@pytest.fixture(scope="module")
def real(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ContainerManager]:
    """One real container for the module: starting one and installing packages takes seconds."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(container_manager, "WORKSPACE_BASE", str(tmp_path_factory.mktemp("ws")))
        with ContainerManager(TEST_IMAGE) as manager:
            yield manager


@pytest.fixture
def env(real: ContainerManager) -> ContainerManager:
    """The shared container with fresh variables and no LM handler."""
    state = Path(real.temp_dir, "state.dill")
    if state.exists():
        state.unlink()
    real.lm_handler_address = None
    real.exec_timeout = 600
    return real


@pytest.mark.docker
def test_real_container_runs_code(env: ContainerManager) -> None:
    result = env.execute_code("print(1 + 1)")
    assert (result.stdout, result.stderr) == ("2\n", "")


@pytest.mark.docker
def test_real_state_persists(env: ContainerManager) -> None:
    env.execute_code("x = 5")
    assert env.execute_code("print(x * 2)").stdout == "10\n"


@pytest.mark.docker
def test_real_llm_query_reaches_host(env: ContainerManager) -> None:
    # Proves that host.docker.internal and the proxy's 0.0.0.0 binding work.
    with LMHandler(MockLM()) as handler:
        env.update_handler_address(handler.address)
        assert env.execute_code('print(llm_query("hi"))').stdout == "Mock: hi\n"


@pytest.mark.docker
def test_real_llm_query_batched(env: ContainerManager) -> None:
    with LMHandler(MockLM()) as handler:
        env.update_handler_address(handler.address)
        assert env.execute_code('print(llm_query_batched(["a", "b"]))').stdout == "['Mock: a', 'Mock: b']\n"


@pytest.mark.docker
def test_real_handler_switch(env: ContainerManager) -> None:
    with LMHandler(PrefixLM("A")) as a, LMHandler(PrefixLM("B")) as b:
        env.update_handler_address(a.address)
        assert env.execute_code('print(llm_query("x"))').stdout == "A:x\n"
        env.update_handler_address(b.address)
        assert env.execute_code('print(llm_query("x"))').stdout == "B:x\n"


@pytest.mark.docker
def test_real_show_vars(env: ContainerManager) -> None:
    env.execute_code("x = 1")
    assert "'x': 'int'" in env.execute_code("print(SHOW_VARS())").stdout


@pytest.mark.docker
def test_real_new_round_resets_answer(env: ContainerManager) -> None:
    env.execute_code('answer["content"] = "round 1"\nanswer["ready"] = True\nkeep = 1')
    env.update_handler_address(("127.0.0.1", 9))
    result = env.execute_code('print(answer["ready"], keep)')
    assert result.stdout == "False 1\n" and result.final_answer is None


@pytest.mark.docker
def test_real_final_answer(env: ContainerManager) -> None:
    result = env.execute_code('answer["content"] = "42"\nanswer["ready"] = True')
    assert result.final_answer == "42"


@pytest.mark.docker
def test_real_output_that_bypasses_capture(env: ContainerManager) -> None:
    result = env.execute_code('import subprocess\nsubprocess.run(["echo", "from echo"])\nprint("done")')
    assert result.stdout == "from echo\ndone\n"


@pytest.mark.docker
def test_real_timeout_keeps_state(env: ContainerManager) -> None:
    env.exec_timeout = 2
    env.execute_code("y = 1")
    result = env.execute_code("y = 2\nwhile True: pass")
    assert result.stderr == "Execution timed out after 2 s"
    assert env.execute_code("print(y)").stdout == "1\n"  # the state from before the block


@pytest.mark.docker
def test_real_timeout_when_sigterm_is_ignored(env: ContainerManager) -> None:
    # `timeout` sends SIGTERM first; -k sends SIGKILL when that is ignored (exit code 137).
    env.exec_timeout = 1
    code = "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: pass"
    result = env.execute_code(code)
    assert result.stderr == "Execution timed out after 1 s"
    assert result.execution_time < 1 + container_manager.KILL_AFTER + 3


@pytest.mark.docker
def test_real_custom_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(container_manager, "WORKSPACE_BASE", str(tmp_path))
    with ContainerManager(TEST_IMAGE, custom_tools={"triple": "def triple(x):\n    return x * 3"}) as m:
        assert m.execute_code("print(triple(4))").stdout == "12\n"


@pytest.mark.docker
def test_real_cleanup_removes_container_and_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(container_manager, "WORKSPACE_BASE", str(tmp_path))
    m = ContainerManager(TEST_IMAGE)
    m.execute_code("import os\nos.makedirs('/workspace/sub')\nopen('/workspace/sub/f', 'w').write('x')\nx = 1")
    m.cleanup()
    inspect = subprocess.run(["docker", "inspect", m.container_id], capture_output=True)
    assert inspect.returncode != 0
    assert os.listdir(tmp_path) == []  # including the files root created in the container
