"""Step 0: the package installs and the test setup works."""

import sys

import pytest

import dockerrepl


def test_package_imports() -> None:
    assert dockerrepl.__version__ == "0.1.0"


def test_python_version_is_supported() -> None:
    assert sys.version_info >= (3, 11)


@pytest.mark.docker
def test_docker_marker_is_registered() -> None:
    assert True
