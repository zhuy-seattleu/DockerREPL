# DockerREPL — Step-by-Step Build Guide

This guide builds the system described in [`../ARCHITECTURE.md`](../ARCHITECTURE.md)
one feature at a time. Every step adds **one component and its unit tests**.
A step is finished only when its own tests pass **and every earlier step's
tests still pass**.

## How the steps are ordered

The order follows the dependencies between components. Each step uses only
what earlier steps built. The parts that need Docker come last, so steps 0–7
run on any machine with Python.

```
 0 setup ─▶ 1 parsing ─▶ 2 LM clients ─▶ 3 wire protocol ─▶ 4 LMHandler
                                                                 │
             5 custom tools ─▶ 6 exec script ◀────────────────────┤
                                     │                           │
                                     ▼                           ▼
                              8 ContainerManager ◀────── 7 LMProxyHandler
                                     │
                                     ▼
                                 9 Client ─▶ 10 end-to-end + example
```

| Step | Feature | New files | Tests | Docker? | Main ideas |
|---|---|---|---|---|---|
| [0](step-00-setup.md) | Project setup + CI | `pyproject.toml`, package skeleton, `tests/conftest.py`, `.github/workflows/tests.yml` | smoke test | no | packaging, virtual environments, pytest, test markers, GitHub Actions |
| [1](step-01-parsing.md) | Extract `repl` code blocks | `parsing.py` | `test_parsing.py` | no | regular expressions |
| [2](step-02-lm-clients.md) | `BaseLM` + `MockLM` | `constants.py`, `clients/` | `test_lm_clients.py` | no | ABCs, template method, semaphores, locks |
| [3](step-03-comms.md) | Length-prefixed JSON over TCP | `comms.py` | `test_comms.py` | no | TCP streams, framing, `struct` |
| [4](step-04-lm-handler.md) | `LMHandler` server | `lm_handler.py` | `test_lm_handler.py` | no | `socketserver`, threads, thread pools |
| [5](step-05-tools.md) | `custom_tools` validation | `environments/tools.py` | `test_tools.py` | no | input validation, JSON |
| [6](step-06-exec-script.md) | Generated `_exec.py` | `environments/exec_script.py` | `test_exec_script.py` | no | code generation, `exec`, `dill` |
| [7](step-07-proxy.md) | `LMProxyHandler` | `environments/proxy.py` | `test_proxy.py` | no | HTTP servers, `type()`, live references |
| [8](step-08-container-manager.md) | `ContainerManager` | `environments/container_manager.py` | `test_container_manager.py` | partly | subprocess, Docker, timeouts, cleanup |
| [9](step-09-client.md) | `Client` round loop | `client.py` | `test_client.py` | no | orchestration, test doubles |
| [10](step-10-end-to-end.md) | End-to-end + example | `examples/docker_repl_example.py` | `test_e2e.py` | yes | integration testing |

## Final layout

```
pyproject.toml
dockerrepl/
  __init__.py
  constants.py             # step 2
  parsing.py               # step 1
  clients/
    __init__.py
    base_lm.py             # step 2
    mock_lm.py             # step 2
  comms.py                 # step 3
  lm_handler.py            # step 4
  environments/
    __init__.py
    tools.py               # step 5
    exec_script.py         # step 6
    proxy.py               # step 7
    container_manager.py   # step 8
  client.py                # step 9
examples/
  docker_repl_example.py   # step 10
tests/
  conftest.py              # step 0
  test_smoke.py            # step 0
  test_parsing.py          # step 1
  ...                      # one test file per step
```

## Conventions for every step

* **Type hints on everything:** every function parameter, return value and
  class attribute. Use the signatures in `ARCHITECTURE.md` exactly. Later
  steps depend on them.
* **Docstrings:** one short docstring per public class and function, saying
  what it does, not how.
* **Test names describe behaviour:** for example
  `test_closed_mid_payload_raises_connection_error`, not `test_recv_3`.
* **No real network except loopback.** Tests talk only to `127.0.0.1`, use
  port `0` so the OS picks a free port, and never call a real LM API.
* **No sleeping to "wait for" things.** If a test needs a server, start it,
  read its real address, and connect. Use `time.sleep` only inside test LMs
  that deliberately simulate slow work.
* **Docker tests carry a marker.** Any test that needs a Docker daemon is
  marked `@pytest.mark.docker`. `tests/conftest.py` (step 0) skips those
  tests automatically when Docker isn't running.
* **Commit once per step**, after all tests pass. A good message is
  `Step 3: length-prefixed JSON framing + tests`.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -q                      # everything (Docker tests skip if no daemon)
pytest -q tests/test_comms.py  # one step
pytest -q -m "not docker"      # never touch Docker
pytest -q -m docker            # only the Docker tests
```

## How each step page is organised

1. **Goal:** what exists after this step.
2. **What you'll learn:** the concepts the step practises.
3. **Depends on:** the earlier steps it uses.
4. **Files:** what to create or change.
5. **Interface:** the exact signatures to implement.
6. **Implementation guide:** numbered hints, in the order to write the code.
7. **Tests:** every test to write, with what it checks.
8. **Run:** the command for this step's tests.
9. **Done when:** a checklist to tick before moving on.
10. **Pitfalls:** mistakes that are easy to make here.
