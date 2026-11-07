"""The first Chrome start of a CI job, measured once before the tests that need Chrome (issue #39,
schema gate REVISE 1/4): one launch, never a relaunch, stopped on every path, its own port. No
desktop: launch, wait, stop and the clock are injected."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from steerai.probes.chrome_a11y import COLD_START_PORT, measure_cold_start
from steerai.probes.windows import WindowInfo

PORTS_IN_TESTS = {9335, 9338, 9339, 9341}


class _Rig:
    def __init__(self, *, window_after: float | None, exit_code: int | None = None) -> None:
        self.now = 0.0
        self.window_after = window_after
        self.exit_code = exit_code
        self.launches: list[tuple[Path, int, str]] = []
        self.stopped: list[Any] = []

    def monotonic(self) -> float:
        return self.now

    def launch(self, _chrome: Path, profile: Path, port: int, url: str, **_k: Any) -> Any:
        self.launches.append((profile, port, url))
        return SimpleNamespace(pid=4242, poll=lambda: self.exit_code)

    def wait(self, pid: int, *, timeout_s: float, process: Any, **_k: Any) -> WindowInfo:
        assert process.poll is not None
        if self.window_after is None or self.window_after > timeout_s:
            self.now += timeout_s
            raise TimeoutError(f"no visible window for pid {pid} within {timeout_s} s")
        self.now += self.window_after
        return WindowInfo(hwnd=0x30, title="about:blank - Google Chrome", pid=pid)

    def stop(self, proc: Any, profile: Path) -> None:
        self.stopped.append((proc.pid, profile))

    def run(self, tmp_path: Path) -> Any:
        return measure_cold_start(
            Path("chrome.exe"),
            tmp_path / "profile",
            launch=self.launch,
            wait=self.wait,
            stop=self.stop,
            monotonic=self.monotonic,
            timeout_s=90.0,
        )


def test_a_window_in_time_is_measured_and_chrome_is_stopped(tmp_path: Path) -> None:
    rig = _Rig(window_after=12.5)
    result = rig.run(tmp_path)
    assert result.seconds == pytest.approx(12.5)
    assert result.error is None
    assert result.summary() == "chrome cold start: 12.5 s (window)"
    assert len(rig.launches) == 1, "one launch, never a relaunch"
    assert rig.stopped == [(4242, tmp_path / "profile")]


def test_no_window_is_a_diagnosis_and_chrome_is_still_stopped(tmp_path: Path) -> None:
    rig = _Rig(window_after=None, exit_code=21)
    result = rig.run(tmp_path)
    assert result.error is not None
    assert "no visible window" in result.error
    assert result.summary().startswith("chrome cold start: no window in 90 s")
    assert len(rig.launches) == 1
    assert rig.stopped == [(4242, tmp_path / "profile")], "stopped on the timeout path too"


def test_a_failed_launch_is_a_diagnosis_with_nothing_to_stop(tmp_path: Path) -> None:
    rig = _Rig(window_after=1.0)

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise OSError("chrome.exe not found")

    rig.launch = refuse  # type: ignore[method-assign]  # a launch that cannot start
    result = rig.run(tmp_path)
    assert result.error is not None
    assert "chrome.exe not found" in result.error
    assert rig.stopped == []


def test_the_warm_up_has_its_own_port_and_a_blank_page(tmp_path: Path) -> None:
    rig = _Rig(window_after=1.0)
    rig.run(tmp_path)
    (_profile, port, url) = rig.launches[0]
    assert port == COLD_START_PORT
    assert port not in PORTS_IN_TESTS
    assert url == "about:blank"
