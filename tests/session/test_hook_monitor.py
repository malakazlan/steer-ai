"""The Win32 shell of the takeover monitor (TK-04/12/13/18/21/33/34/39/44) and the kill-key
parser. Hook and hot-key tests touch the real desktop and are Windows-marked; the parser is pure."""

from __future__ import annotations

import ctypes
import threading
import time

import pytest

from steerai.action.keyboard import chord_signature
from steerai.errors import ErrorCode, SteerError
from steerai.session.monitor import DEFAULT_KILL_KEY, HookMonitor, parse_kill_key

# ---- the kill key (pure) ------------------------------------------------------------------------


def test_the_default_kill_key_needs_three_modifiers_and_parses() -> None:
    mods, vk = parse_kill_key(DEFAULT_KILL_KEY)
    assert mods == frozenset({"ctrl", "alt", "shift"})
    assert vk == ord("S")


@pytest.mark.parametrize(
    "chord",
    [
        "s",
        "f12",
        "ctrl+f12",
        "alt+f4",
        "alt+shift+tab",
        "ctrl+v",
        "win+l",
        "",
        "ctrl+",
        "ctrl+bogus",
    ],
)
def test_bare_keys_f12_blocked_and_routed_chords_are_refused(chord: str) -> None:
    with pytest.raises(ValueError, match="kill key"):
        parse_kill_key(chord)


def test_a_plain_chord_with_one_modifier_is_accepted() -> None:
    assert parse_kill_key("ctrl+alt+k") == chord_signature("ctrl+alt+k")


# ---- the hook thread (Windows) -------------------------------------------------------------


pytestmark_windows = pytest.mark.windows


@pytest.mark.windows
def test_start_close_start_in_one_process_and_close_is_bounded() -> None:
    first = HookMonitor()
    first.start()
    try:
        assert first.healthy() is True
    finally:
        started = time.perf_counter()
        first.close()
        assert time.perf_counter() - started < 2.5
    second = HookMonitor()
    second.start()
    second.close()
    second.close()  # idempotent


@pytest.mark.windows
def test_our_own_injection_is_steer_and_the_self_probe_keeps_it_healthy() -> None:
    monitor = HookMonitor()
    monitor.start()
    try:
        start = monitor.last_user_input()
        monitor.probe_now()
        time.sleep(0.3)
        assert monitor.counts().get("steer", 0) >= 1
        assert monitor.healthy() is True
        assert monitor.last_user_input() == start, "a tagged event is never the person"
    finally:
        monitor.close()


@pytest.mark.windows
def test_the_hot_key_sets_the_sticky_kill_flag() -> None:
    monitor = HookMonitor()
    monitor.start()
    try:
        assert monitor.kill_requested() is False
        monitor.press_kill_key_for_test()  # a tagged SendInput of the registered chord
        deadline = time.monotonic() + 2.0
        while not monitor.kill_requested() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert monitor.kill_requested() is True
        monitor.press_kill_key_for_test()
        time.sleep(0.1)
        assert monitor.kill_requested() is True
    finally:
        monitor.close()


@pytest.mark.windows
def test_a_hot_key_already_taken_is_a_start_refusal_with_the_token() -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    mods, vk = parse_kill_key(DEFAULT_KILL_KEY)
    flags = {"alt": 1, "ctrl": 2, "shift": 4, "win": 8}
    fs = sum(flags[m] for m in mods) | 0x4000
    assert user32.RegisterHotKey(None, 0x0ABC, fs, vk), "the test thread takes the key first"
    try:
        monitor = HookMonitor()
        with pytest.raises(SteerError) as info:
            monitor.start()
        assert info.value.code is ErrorCode.GATE_BLOCKED
        assert "kill_key_unavailable" in info.value.message
        monitor.close()
    finally:
        user32.UnregisterHotKey(None, 0x0ABC)


@pytest.mark.windows
def test_a_removed_hook_is_detected_by_the_self_probe() -> None:
    monitor = HookMonitor()
    monitor.start()
    try:
        monitor.unhook_for_test()
        monitor.probe_now()
        time.sleep(0.4)
        assert monitor.healthy() is False
        assert monitor.detail() == "monitor_lost"
    finally:
        monitor.close()


@pytest.mark.windows
def test_the_hook_thread_is_a_daemon_and_close_from_another_thread_works() -> None:
    monitor = HookMonitor()
    monitor.start()
    assert monitor.thread_is_daemon() is True
    done = threading.Event()

    def closer() -> None:
        monitor.close()
        done.set()

    threading.Thread(target=closer).start()
    assert done.wait(timeout=5.0)
