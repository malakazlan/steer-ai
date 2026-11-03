"""Keyboard injection: Unicode text and named key chords, every event tagged (design 5.9)."""

import pytest

from steerai.action.keyboard import (
    BLOCKED_COMBOS,
    chord_to_vks,
    is_blocked_combo,
    text_to_inputs,
)
from steerai.probes.win_input import STEER_EXTRA_INFO


def test_text_to_inputs_emits_unicode_down_up_pairs_with_the_tag() -> None:
    records = text_to_inputs("hi")
    assert len(records) == 4  # h down, h up, i down, i up
    assert all(r.union.ki.dwExtraInfo == STEER_EXTRA_INFO for r in records)
    assert records[0].union.ki.wScan == ord("h")
    assert records[0].union.ki.dwFlags & 0x0004  # KEYEVENTF_UNICODE
    assert records[1].union.ki.dwFlags & 0x0002  # KEYEVENTF_KEYUP


def test_text_to_inputs_splits_astral_characters_into_surrogates() -> None:
    records = text_to_inputs("👍")
    assert len(records) == 4
    assert records[0].union.ki.wScan == 0xD83D
    assert records[2].union.ki.wScan == 0xDC4D


def test_chord_to_vks_maps_modifiers_and_named_keys() -> None:
    assert chord_to_vks("shift+enter") == [0x10, 0x0D]
    assert chord_to_vks("ctrl+s") == [0x11, 0x53]
    assert chord_to_vks("Enter") == [0x0D]


def test_chord_to_vks_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError, match="unknown key"):
        chord_to_vks("ctrl+bogus")


@pytest.mark.parametrize("combo", ["alt+f4", "win+l", "ALT+TAB", "ctrl+shift+esc", "alt+shift+tab"])
def test_dangerous_combos_are_blocked_including_supersets(combo: str) -> None:
    assert is_blocked_combo(combo) is True


@pytest.mark.parametrize("combo", ["ctrl+c", "enter", "shift+enter", "ctrl+s"])
def test_ordinary_combos_are_not_blocked(combo: str) -> None:
    assert is_blocked_combo(combo) is False


def test_blocked_list_matches_the_design() -> None:
    assert {
        "alt+f4",
        "win+l",
        "win+d",
        "alt+tab",
        "win+r",
        "win+x",
        "win+e",
        "ctrl+shift+esc",
    } <= set(BLOCKED_COMBOS)
