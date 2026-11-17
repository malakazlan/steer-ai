"""#492, [V2.80]: how a probe's events are built (pure: nothing is sent)."""

from __future__ import annotations

import ctypes

import pytest

from steerai.probes import win_input
from steerai.session.hook_classifier import probe_tag

_NOCOALESCE = 0x2000
"""MOUSEEVENTF_MOVE_NOCOALESCE (Microsoft Learn, MOUSEINPUT): the move is not merged with a
later one, so a numbered probe move is never dropped for a newer move."""


def test_only_the_numbered_nudge_asks_not_to_coalesce() -> None:
    assert win_input.nudge_flags(seq=None) & _NOCOALESCE == 0
    numbered = win_input.nudge_flags(seq=5)
    assert numbered & _NOCOALESCE
    assert numbered & win_input._MOUSEEVENTF_MOVE
    assert numbered & win_input._MOUSEEVENTF_ABSOLUTE


def test_the_probe_tag_helper_binds_the_run_tag_and_the_number() -> None:
    assert win_input._probe_tag_of(0x1111, 3) == probe_tag(0x1111, 3)
    assert win_input._probe_tag_of(0x1111, None) == 0x1111
    assert win_input._probe_tag_of(None, None) is None


def test_a_32_bit_dword_cannot_carry_a_number(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctypes, "sizeof", lambda _t: 4)
    with pytest.raises(OSError, match="64-bit"):
        win_input._probe_tag_of(0x1111, 3)
