"""`acquire_foreground` never presses ALT (issue #38, hunter SF-03): a lone ALT tap puts the window
entered in menu mode (measured 2026-11-07, classic Notepad: 16 of 17 ALT taps), so its next typed
keys would be menu mnemonics; a tagged zero-distance mouse move gives the last input the same way
(SetForegroundWindow: "the calling process received the last input event") and enters no menu
(20 of 20 in front, 0 in menu mode)."""

from __future__ import annotations

from typing import Any

import pytest

from steerai.probes import win_input


class _User32:
    def __init__(self, *, front_after: int) -> None:
        self.front = 0x99
        self.calls = 0
        self._front_after = front_after

    def GetForegroundWindow(self) -> int:  # noqa: N802 - the Win32 name
        return self.front

    def SetForegroundWindow(self, hwnd: int) -> int:  # noqa: N802 - the Win32 name
        self.calls += 1
        if self.calls >= self._front_after:
            self.front = hwnd
        return 1


def _patched(monkeypatch: pytest.MonkeyPatch, user32: Any) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(win_input, "_user32", lambda: user32)
    monkeypatch.setattr(win_input, "key_tap", lambda vk: seen.append(f"key {vk:#x}"))
    monkeypatch.setattr(win_input, "nudge_in_place", lambda: seen.append("nudge"))
    return seen


def test_the_second_rung_is_a_nudge_never_an_alt_tap(monkeypatch: pytest.MonkeyPatch) -> None:
    user32 = _User32(front_after=2)
    seen = _patched(monkeypatch, user32)
    assert win_input.acquire_foreground(0x30) == "input-nudge"
    assert seen == ["nudge"]


def test_a_direct_call_that_works_sends_no_input(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patched(monkeypatch, _User32(front_after=1))
    assert win_input.acquire_foreground(0x30) == "direct"
    assert seen == []
