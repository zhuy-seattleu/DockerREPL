# Step 0 — Project setup

## Goal

A clean, installable Python project with an automated test harness, as the
base for every later step. After this step, `pytest -q` passes, any test
marked `docker` is skipped automatically on machines without a running Docker
daemon, and GitHub runs the tests on every push.

## Tasks at a glance

1. **Create the package structure.** Make the top-level `dockerrepl/`
   directory and its subpackages `clients/` (the LM clients) and
   `environments/` (the Docker-side components). Each gets an `__init__.py`,
   which makes it a Python package, and `dockerrepl/__init__.py` records the
   version, `__version__ = "0.1.0"`.
2. **Write `pyproject.toml`.** It declares the project's name, version and
   required Python (3.11+), and its runtime dependencies, `requests` and
   `dill`. `pytest` goes in a separate `dev` group of developer-only
   dependencies, and the file also holds pytest's settings.
3. **Set up an isolated environment.** Create a virtual environment once with
   `python -m venv .venv`, activate it, and install the project with
   `pip install -e ".[dev]"`. pip reads the dependencies from
   `pyproject.toml` and installs the project in editable mode, so code changes
   take effect without reinstalling.
4. **Configure the test framework.** Write `tests/conftest.py`, which pytest
   loads automatically. It detects whether a Docker daemon is running and
   automatically skips tests labelled `@pytest.mark.docker` when it isn't, so
   the suite runs cleanly on any machine.
5. **Write the first tests.** `tests/test_smoke.py` checks that the package
   imports with the right version, that Python is 3.11 or newer, and that the
   `docker` test label is registered.
6. **Set up continuous integration.** Add a GitHub Actions workflow,
   `.github/workflows/tests.yml`, and a script, `scripts/run_step_tests.sh`,
   that run each build step's tests on every push. Each run produces a
   per-step ✅/❌ table, and GitHub's machines have Docker, so the Docker
   tests really run there.
7. **Keep the repository clean.** A `.gitignore` excludes generated files
   such as `.venv/`, `__pycache__/` and build metadata.

**Outcome:** `pytest` reports **2 passed, 1 skipped** on a machine without
Docker, and **3 passed** with Docker, including on GitHub Actions.

### How the repository, `pyproject.toml` and `.venv` fit together

```
                 GitHub repo (shared, versioned)
   ┌─────────────────────────────────────────────────────┐
   │  dockerrepl/  tests/  pyproject.toml  .gitignore    │
   │  .github/workflows/tests.yml                        │
   │  (no .venv: .gitignore excludes it)                 │
   └───────────────┬─────────────────────┬───────────────┘
        git clone / git pull             │ on every push
                   ▼                     ▼
   Your laptop / Codespace           GitHub Actions machine
   ───────────────────────           ──────────────────────
   python -m venv .venv  ← once      fresh Python (setup-python)
   pip install -e ".[dev]"           pip install -e ".[dev]"
     reads pyproject.toml              reads pyproject.toml
   .venv/ = local, never pushed      thrown away after the run
   pytest                            pytest → ✅/❌ shown on GitHub
```

* **The repository holds what is shared:** source code, tests,
  `pyproject.toml` and the workflow.
* **Each machine builds its own environment.** `.venv` contains a copy of
  Python for one operating system, so it is never committed. Cloning doesn't
  create it; you create it once per clone.
* **`pyproject.toml` connects the two.** Every clone, Codespace and CI run
  installs the same dependencies from it with one command. When it changes,
  `git pull` and re-run `pip install -e ".[dev]"`.

## What you'll learn

* **Packaging:** a minimal `pyproject.toml`, and installing your own package in
  editable mode (`pip install -e`).
* **pytest basics:** test discovery, `conftest.py`, and custom markers.
* **Skipping tests by environment:** decide at collection time whether a
  resource (Docker) is available.

## Depends on

Nothing.

## Files

| File | Purpose |
|---|---|
| `pyproject.toml` | package metadata, dependencies, pytest settings |
| `dockerrepl/__init__.py` | makes `dockerrepl` a package; holds `__version__` |
| `dockerrepl/clients/__init__.py` | empty, so the subpackage exists for step 2 |
| `dockerrepl/environments/__init__.py` | empty, so the subpackage exists for step 5 |
| `tests/conftest.py` | the `docker` marker's skip logic |
| `tests/test_smoke.py` | proves the package imports and the Python version is right |
| `.github/workflows/tests.yml` | GitHub Actions: install the project and run the tests on every push, plus a manual "Run workflow" button |
| `scripts/run_step_tests.sh` | runs each step's test file separately and writes a per-step ✅/❌ table (steps not built yet are listed, not failed) |
| `.gitignore` | `.venv/`, `__pycache__/`, `*.egg-info/`, `.pytest_cache/`, `.dockerrepl_workspace/` |

## Interface

`pyproject.toml` must contain:

```toml
[project]
name = "dockerrepl"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["requests>=2.31", "dill>=0.3.8"]

[project.optional-dependencies]
dev = ["pytest>=8"]

[tool.setuptools.packages.find]
include = ["dockerrepl*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["docker: needs a running Docker daemon"]
```

`tests/conftest.py` exposes:

```python
def docker_available() -> bool: ...
def pytest_collection_modifyitems(config: pytest.Config,
                                  items: list[pytest.Item]) -> None: ...
```

## Implementation guide

1. Create the folders and empty `__init__.py` files. In
   `dockerrepl/__init__.py` put `__version__ = "0.1.0"`.
2. Write `pyproject.toml` as above. Add a `[build-system]` table using
   setuptools (`requires = ["setuptools>=68"]`,
   `build-backend = "setuptools.build_meta"`).
3. `docker_available()` runs `docker info` with
   `subprocess.run([...], capture_output=True, timeout=10)`. It returns `True`
   only if the exit code is 0. It returns `False` if the command is missing
   (`FileNotFoundError`) or times out (`subprocess.TimeoutExpired`). Cache the
   result with `functools.lru_cache`, so `docker info` runs at most once per
   test session.
4. `pytest_collection_modifyitems` loops over `items`. If an item has the
   `docker` marker (`item.get_closest_marker("docker")`) and
   `docker_available()` is `False`, it adds
   `pytest.mark.skip(reason="Docker daemon not available")`.
5. Create a virtual environment and run `pip install -e ".[dev]"`.
6. Add the workflow and `scripts/run_step_tests.sh`. The workflow checks out
   the code, sets up Python 3.12, runs `pip install -e ".[dev]"`, then
   `bash scripts/run_step_tests.sh`. The script exits with 1 if any built
   step fails, so the run shows ❌.

## Tests — `tests/test_smoke.py`

| Test | Checks |
|---|---|
| `test_package_imports` | `import dockerrepl` works and `dockerrepl.__version__ == "0.1.0"` |
| `test_python_version_is_supported` | `sys.version_info >= (3, 11)` |
| `test_docker_marker_is_registered` | a test marked `@pytest.mark.docker` with body `assert True` runs without an "unknown marker" warning. It passes when Docker runs and is skipped otherwise. |

## Run

Locally, after creating and activating `.venv`:

```bash
pip install -e ".[dev]"
pytest -v -rs                       # -v: one line per test; -rs: say why tests were skipped
bash scripts/run_step_tests.sh      # the same per-step table CI shows (macOS/Linux, or Git Bash on Windows)
```

On GitHub, push the branch. Then open the **Actions** tab, click the run, and
read the **"Tests by build step"** table.

## Done when

- [ ] `pip install -e ".[dev]"` succeeds in a fresh virtual environment.
- [ ] `pytest -q` passes, with the Docker test either passed or skipped.
- [ ] `pytest -q -W error::pytest.PytestUnknownMarkWarning` also passes.
- [ ] `git status` shows no `__pycache__` or `.egg-info` files.
- [ ] The **Tests** workflow on GitHub is green, and its table shows step 0 as ✅.

## Pitfalls

* **Forgetting `-e`.** Without an editable install, edits to `dockerrepl/`
  aren't seen by the tests until you reinstall.
* **Running `docker info` once per test.** Without the cache it can take
  seconds per test.
* **Raising instead of returning `False`** when `docker` isn't installed.
  That crashes collection on machines without Docker.
