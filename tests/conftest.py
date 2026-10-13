"""Shared pytest configuration: skip @pytest.mark.docker tests when Docker isn't running."""

import functools
import subprocess

import pytest


@functools.lru_cache(maxsize=1)
def docker_available() -> bool:
    """Return True if `docker info` succeeds, i.e. a Docker daemon is reachable."""
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark every `docker` test as skipped when no Docker daemon is available."""
    skip_docker = pytest.mark.skip(reason="Docker daemon not available")
    for item in items:
        if item.get_closest_marker("docker") and not docker_available():
            item.add_marker(skip_docker)
