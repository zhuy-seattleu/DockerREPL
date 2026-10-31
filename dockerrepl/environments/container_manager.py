"""ContainerManager: one Docker container, its proxy, and running code blocks in it.

The container is started once and kept idle (`tail -f /dev/null`). Each code
block becomes a fresh `_exec.py` (step 6), written to a host folder that the
container sees as /workspace, and is run with `docker exec`. Variables survive
between blocks in /workspace/state.dill. Calls to `llm_query` reach the host
through the proxy (step 7), which forwards them to the current LMHandler.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from dockerrepl.environments.exec_script import MAX_OUTPUT_CHARS, _build_exec_script
from dockerrepl.environments.proxy import ThreadingProxyServer, start_proxy_server
from dockerrepl.environments.tools import validate_custom_tools

# Host folder that holds each container's workspace. It lives in the current
# directory because Docker Desktop (macOS, Windows) can only mount folders it
# shares, and the system temp folder may not be one of them.
WORKSPACE_BASE: str = ".dockerrepl_workspace"
# Seconds allowed for setup commands; `docker run` may have to pull the image first.
SETUP_TIMEOUT: int = 600
# Seconds allowed for each cleanup command.
CLEANUP_TIMEOUT: int = 30
# After the time limit, `timeout` sends SIGTERM; if the process is still alive
# this many seconds later, it sends SIGKILL.
KILL_AFTER: int = 5
# Exit codes of `timeout`: 124 after SIGTERM worked, 137 (128 + 9) after SIGKILL.
TIMEOUT_EXIT_CODES: frozenset[int] = frozenset({124, 137})


@dataclass
class REPLResult:
    """What one execute_code call produced."""

    stdout: str
    stderr: str
    locals: dict[str, str] = field(default_factory=dict)  # name -> repr(value)
    final_answer: str | None = None  # set when the code sets answer["ready"] = True
    execution_time: float = 0.0


def _cap(text: str) -> str:
    """Keep the start and end of long text, as the exec script does."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return text[:half] + f"\n[... {len(text) - 2 * half} characters cut ...]\n" + text[-half:]


class ContainerManager:
    """Owns one container and one proxy. Use it as a context manager, or call cleanup()."""

    def __init__(
        self,
        image: str = "python:3.11-slim",
        lm_handler_address: tuple[str, int] | None = None,
        persistent: bool = False,
        custom_tools: dict[str, Any] | None = None,
        exec_timeout: int = 600,
    ) -> None:
        validate_custom_tools(custom_tools)  # bad tools fail before any Docker command runs
        self.image: str = image
        self.lm_handler_address: tuple[str, int] | None = lm_handler_address  # read live by the proxy
        self.persistent: bool = persistent
        self.custom_tools: dict[str, Any] = custom_tools or {}
        self.exec_timeout: int = exec_timeout
        self.container_id: str | None = None
        self.temp_dir: str = ""
        self.proxy_server: ThreadingProxyServer | None = None
        self.proxy_thread: threading.Thread | None = None
        self.proxy_port: int = 0
        self._new_round: bool = False
        self._cleaned_up: bool = False
        try:
            self.setup()
        except BaseException:
            self.cleanup()  # a half-built manager leaves nothing behind
            raise

    # ------------------------------------------------------------ setup

    def setup(self) -> None:
        """Make the workspace, start the proxy and the container, and install dill and requests."""
        os.makedirs(WORKSPACE_BASE, exist_ok=True)
        self.temp_dir = tempfile.mkdtemp(prefix="dockerrepl_", dir=os.path.abspath(WORKSPACE_BASE))
        self.proxy_server, self.proxy_thread = start_proxy_server(self, "0.0.0.0", 0)
        self.proxy_port = self.proxy_server.server_address[1]

        run = self._docker(
            "run", "-d", "--rm",
            "-v", f"{self.temp_dir}:/workspace",
            "--add-host", "host.docker.internal:host-gateway",
            self.image, "tail", "-f", "/dev/null",
            timeout=SETUP_TIMEOUT,
        )  # fmt: skip
        if run.returncode != 0:
            raise RuntimeError(f"docker run failed: {run.stderr.strip()}")
        self.container_id = run.stdout.strip()

        check = ["exec", self.container_id, "python", "-c", "import dill, requests"]
        if self._docker(*check, timeout=SETUP_TIMEOUT).returncode != 0:  # skip pip if already there
            self._docker("exec", self.container_id, "pip", "install", "-q", "dill", "requests", timeout=SETUP_TIMEOUT)
            again = self._docker(*check, timeout=SETUP_TIMEOUT)
            if again.returncode != 0:
                raise RuntimeError(f"could not install dill/requests in {self.image}: {again.stderr.strip()}")

    @staticmethod
    def _docker(*args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise RuntimeError("the docker command was not found; is Docker installed?") from None

    # ------------------------------------------------------------ running code

    def execute_code(self, code: str) -> REPLResult:
        """Run one code block in the container. Never raises for problems in the code itself.

        Calls must not overlap: each one writes the same /workspace/_exec.py.
        """
        if self.container_id is None or self._cleaned_up:
            raise RuntimeError("the container is not running")
        start = time.perf_counter()
        script = _build_exec_script(
            code,
            f"http://host.docker.internal:{self.proxy_port}",
            self.custom_tools,
            new_round=self._new_round,
        )
        with open(os.path.join(self.temp_dir, "_exec.py"), "w", encoding="utf-8") as f:
            f.write(script)

        command = [
            "exec", self.container_id,
            "timeout", "-k", str(KILL_AFTER), str(self.exec_timeout),
            "python", "/workspace/_exec.py",
        ]  # fmt: skip
        try:
            proc = self._docker(*command, timeout=self.exec_timeout + KILL_AFTER + 10)
        except subprocess.TimeoutExpired:  # the docker command itself hung
            return self._timed_out(start)
        if proc.returncode in TIMEOUT_EXIT_CODES:
            return self._timed_out(start)

        result = self._parse(proc, start)
        if result is not None:
            # The script ran to the end and saved its state, so the new round's
            # fresh answer is now in state.dill. Until then, keep asking for it.
            self._new_round = False
            return result
        return REPLResult(
            stdout=_cap(proc.stdout),
            stderr=_cap(proc.stderr.strip() or "Could not parse execution result"),
            execution_time=time.perf_counter() - start,
        )

    def _timed_out(self, start: float) -> REPLResult:
        return REPLResult(
            stdout="",
            stderr=f"Execution timed out after {self.exec_timeout} s",
            execution_time=time.perf_counter() - start,
        )

    @staticmethod
    def _parse(proc: subprocess.CompletedProcess[str], start: float) -> REPLResult | None:
        """Build a REPLResult from the script's JSON line, or return None if there isn't one."""
        text = proc.stdout.rstrip("\n")
        cut = text.rfind("\n")  # the newline the script writes just before its JSON line
        try:
            data = json.loads(text[cut + 1 :])
        except ValueError:
            return None
        if not isinstance(data, dict) or not {"stdout", "stderr"} <= data.keys():
            return None
        # Anything before that newline was written straight to the real stdout
        # (for example by a subprocess), bypassing the script's capture.
        raw = text[:cut] if cut >= 0 else ""
        if raw and not raw.endswith("\n"):
            raw += "\n"
        stdout = raw + data["stdout"]
        stderr = data["stderr"] + proc.stderr  # e.g. a warning from docker itself
        return REPLResult(
            stdout=_cap(stdout),
            stderr=_cap(stderr),
            locals=data.get("locals") or {},
            final_answer=data.get("final_answer"),
            execution_time=time.perf_counter() - start,
        )

    # ------------------------------------------------------------ rounds

    def update_handler_address(self, address: tuple[str, int]) -> None:
        """Point the proxy at this round's LMHandler and start the round with a fresh answer."""
        self.lm_handler_address = address  # the proxy reads it on its next request
        self._new_round = True  # the next code block resets `answer` in the container

    # ------------------------------------------------------------ cleanup

    def cleanup(self) -> None:
        """Remove the container, stop the proxy and delete the workspace. Safe to call twice."""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        if self.container_id is not None:
            try:
                # Files created by root in the container may not be deletable from the host.
                self._docker("exec", self.container_id, "find", "/workspace", "-mindepth", "1", "-delete",
                             timeout=CLEANUP_TIMEOUT)  # fmt: skip
                self._docker("rm", "-f", self.container_id, timeout=CLEANUP_TIMEOUT)
            except (subprocess.TimeoutExpired, RuntimeError):
                pass  # carry on: the proxy and the folder must still be cleaned up
        if self.proxy_server is not None:
            self.proxy_server.shutdown()
            self.proxy_server.server_close()
            if self.proxy_thread is not None:
                self.proxy_thread.join(timeout=5)
        if self.temp_dir:
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def __enter__(self) -> "ContainerManager":
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()
