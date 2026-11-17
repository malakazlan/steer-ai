"""[V2.80]: the leak check that runs after every Windows test sees a monitor thread left alive,
and a watch thread, and nothing else."""

from __future__ import annotations

import threading

import pytest

from tests import _leaks


def _alive(name: str) -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    thread = threading.Thread(target=stop.wait, name=name, daemon=True)
    thread.start()
    return thread, stop


@pytest.mark.parametrize("name", _leaks.LEAK_THREAD_NAMES)
def test_a_monitor_or_watch_thread_left_alive_is_a_leak(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_leaks, "JOIN_S", 0.05)
    thread, stop = _alive(name)
    try:
        assert _leaks.leaked_threads() == [name]
    finally:
        stop.set()
        thread.join(timeout=5)
    assert _leaks.leaked_threads() == []


def test_an_unrelated_thread_is_not_a_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_leaks, "JOIN_S", 0.05)
    thread, stop = _alive("something-else")
    try:
        assert _leaks.leaked_threads() == []
    finally:
        stop.set()
        thread.join(timeout=5)


def test_a_thread_that_is_ending_is_given_its_join_window() -> None:
    done = threading.Event()
    thread = threading.Thread(target=done.wait, args=(0.2,), name="steer-monitor", daemon=True)
    thread.start()
    assert _leaks.leaked_threads() == []
