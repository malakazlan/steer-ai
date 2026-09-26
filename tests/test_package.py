"""Smoke tests: the package imports and the toolchain gates are wired."""

import sys

import pytest

import steerai


def test_version_is_semver() -> None:
    major, minor, patch = steerai.__version__.split(".")
    assert all(part.isdigit() for part in (major, minor, patch))


@pytest.mark.windows
def test_windows_marker_runs_only_on_windows() -> None:
    assert sys.platform == "win32"
