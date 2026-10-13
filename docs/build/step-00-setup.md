# Step 0 — Project setup

## Goal

An installable, empty `dockerrepl` package and a test runner that works. After
this step, `pytest -q` passes with one smoke test, and any test marked
`docker` is skipped automatically on machines without a running Docker daemon.

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

## Tests — `tests/test_smoke.py`

| Test | Checks |
|---|---|
| `test_package_imports` | `import dockerrepl` works and `dockerrepl.__version__ == "0.1.0"` |
| `test_python_version_is_supported` | `sys.version_info >= (3, 11)` |
| `test_docker_marker_is_registered` | a test marked `@pytest.mark.docker` with body `assert True` runs without an "unknown marker" warning. It passes when Docker runs and is skipped otherwise. |

## Run

```bash
pip install -e ".[dev]"
pytest -q
```

## Done when

- [ ] `pip install -e ".[dev]"` succeeds in a fresh virtual environment.
- [ ] `pytest -q` passes, with the Docker test either passed or skipped.
- [ ] `pytest -q -W error::pytest.PytestUnknownMarkWarning` also passes.
- [ ] `git status` shows no `__pycache__` or `.egg-info` files.

## Pitfalls

* **Forgetting `-e`.** Without an editable install, edits to `dockerrepl/`
  aren't seen by the tests until you reinstall.
* **Running `docker info` once per test.** Without the cache it can take
  seconds per test.
* **Raising instead of returning `False`** when `docker` isn't installed.
  That crashes collection on machines without Docker.
