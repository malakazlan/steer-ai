"""Test Chrome profiles are removed after the test (349 leaked folders, 3.3 GB in %TEMP%, found
2026-11-07): Chrome's crash reporter outlives the browser and holds a file open, so a delete
right after `terminate()` failed and `ignore_errors=True` hid it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from steerai.probes.chrome_a11y import stop_chrome


class _Proc:
    pid = 4242

    def __init__(self) -> None:
        self.waited = False

    def wait(self, timeout: float) -> int:
        self.waited = timeout > 0
        return 0


def test_the_whole_tree_is_killed_and_the_profile_removed(tmp_path: Path) -> None:
    profile = tmp_path / "steer-x"
    (profile / "Default").mkdir(parents=True)
    calls: list[list[str]] = []

    def run(argv: list[str], **_kw: Any) -> None:
        calls.append(argv)

    stop_chrome(_Proc(), profile, run=run, sleep=lambda _s: None)
    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]], "the tree, crash reporter too"
    assert not profile.exists()


def test_a_held_file_is_retried_until_released(tmp_path: Path) -> None:
    profile = tmp_path / "steer-y"
    profile.mkdir()
    attempts = {"n": 0}

    def remove(path: Path) -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError("CrashpadMetrics-active.pma is in use")
        path.rmdir()

    stop_chrome(_Proc(), profile, run=lambda *_a, **_k: None, sleep=lambda _s: None, remove=remove)
    assert attempts["n"] == 3
    assert not profile.exists()


def test_a_profile_that_cannot_be_removed_warns_never_silently(tmp_path: Path) -> None:
    profile = tmp_path / "steer-z"
    profile.mkdir()

    def remove(_path: Path) -> None:
        raise PermissionError("still in use")

    clock = {"t": 0.0}

    def sleep(seconds: float) -> None:
        clock["t"] += seconds

    with pytest.warns(ResourceWarning, match="steer-z"):
        stop_chrome(
            _Proc(),
            profile,
            run=lambda *_a, **_k: None,
            sleep=sleep,
            remove=remove,
            monotonic=lambda: clock["t"],
        )
    assert clock["t"] >= 5.0, "retried for the release window before warning"
