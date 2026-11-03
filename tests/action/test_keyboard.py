"""Keyboard injection: Unicode text and named key chords, every event tagged (design 5.9)."""

import pytest

from steerai.action import keyboard
from steerai.action.keyboard import (
    BLOCKED_COMBOS,
    GateApproval,
    chord_to_inputs,
    chord_to_vks,
    is_blocked_combo,
    press,
    text_to_inputs,
)
from steerai.errors import ErrorCode, SteerError
from steerai.probes import win_input
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
    with pytest.raises(SteerError, match="unknown key"):
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


@pytest.mark.parametrize("chord", ["", "+", "++", " ", " + "])
def test_empty_chord_is_rejected_by_both_entry_points(chord: str) -> None:
    with pytest.raises(SteerError, match="empty chord"):
        chord_to_vks(chord)
    with pytest.raises(SteerError, match="empty chord"):
        is_blocked_combo(chord)


def test_repeated_and_aliased_modifiers_collapse_to_one_key_down() -> None:
    assert chord_to_vks("ctrl+ctrl+c") == [0x11, 0x43]
    assert chord_to_vks("win+meta+cmd+l") == [0x5B, 0x4C]


@pytest.mark.parametrize("chord", ["super+c", "hyper+shift+a", "control+s"])
def test_unknown_modifier_is_rejected(chord: str) -> None:
    with pytest.raises(SteerError, match="unknown modifier"):
        chord_to_vks(chord)
    with pytest.raises(SteerError, match="unknown modifier"):
        is_blocked_combo(chord)


@pytest.mark.parametrize("chord", ["ctrl", "ctrl+", "shift+alt"])
def test_modifier_only_chord_is_rejected_as_unknown_key(chord: str) -> None:
    with pytest.raises(SteerError, match="unknown key"):
        chord_to_vks(chord)


@pytest.mark.parametrize("chord", ["ctrl+é", "ctrl+ab", "ctrl+ß", "f13", "ctrl+-"])
def test_non_ascii_multichar_or_unmapped_keys_are_rejected(chord: str) -> None:
    with pytest.raises(SteerError, match="unknown key"):
        chord_to_vks(chord)


def test_digits_and_function_key_boundaries_map_to_virtual_keys() -> None:
    assert chord_to_vks("ctrl+1") == [0x11, 0x31]
    assert chord_to_vks("0") == [0x30]
    assert chord_to_vks("f1") == [0x70]
    assert chord_to_vks("f12") == [0x7B]


def test_chord_whitespace_and_case_are_normalised() -> None:
    assert chord_to_vks(" Ctrl + S ") == [0x11, 0x53]
    assert is_blocked_combo(" ALT + F4 ") is True


def test_modifiers_are_emitted_in_canonical_order_regardless_of_spelling() -> None:
    assert chord_to_vks("alt+shift+ctrl+a") == [0x11, 0x10, 0x12, 0x41]
    assert chord_to_vks("shift+win+e") == [0x10, 0x5B, 0x45]


@pytest.mark.parametrize("chord", ["meta+l", "cmd+d", "cmd+shift+r", "Meta+E", "ctrl+meta+x"])
def test_meta_and_cmd_aliases_hit_the_win_blocklist(chord: str) -> None:
    assert is_blocked_combo(chord) is True


@pytest.mark.parametrize(
    ("chord", "blocked"),
    [
        ("ctrl+alt+f4", True),
        ("win+shift+e", True),
        ("ctrl+shift+alt+tab", True),
        ("win+ctrl+d", True),
        ("shift+tab", False),
        ("ctrl+l", False),
        ("alt+f5", False),
        ("f4", False),
    ],
)
def test_superset_rule_only_adds_modifiers_never_changes_the_key(chord: str, blocked: bool) -> None:
    assert is_blocked_combo(chord) is blocked


def test_chord_to_inputs_presses_in_order_and_releases_in_reverse() -> None:
    records = chord_to_inputs("ctrl+shift+s")
    assert [r.union.ki.wVk for r in records] == [0x11, 0x10, 0x53, 0x53, 0x10, 0x11]
    assert [r.union.ki.dwFlags for r in records] == [0, 0, 0, 0x2, 0x2, 0x2]
    assert all(r.union.ki.dwExtraInfo == STEER_EXTRA_INFO for r in records)
    assert all(r.type == 1 for r in records)


def test_text_to_inputs_of_empty_text_is_refused() -> None:
    with pytest.raises(SteerError, match="nothing to type"):
        text_to_inputs("")


def test_text_to_inputs_bmp_and_astral_boundaries() -> None:
    assert [r.union.ki.wScan for r in text_to_inputs("￿")] == [0xFFFF, 0xFFFF]
    assert [r.union.ki.wScan for r in text_to_inputs("\U0010ffff")] == [
        0xDBFF,
        0xDBFF,
        0xDFFF,
        0xDFFF,
    ]
    assert [r.union.ki.wScan for r in text_to_inputs("\U00010000")] == [
        0xD800,
        0xD800,
        0xDC00,
        0xDC00,
    ]


def test_text_to_inputs_never_sets_a_virtual_key() -> None:
    assert all(r.union.ki.wVk == 0 for r in text_to_inputs("a1\t"))


def test_press_refuses_a_blocked_combo_before_touching_sendinput(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[object] = []
    monkeypatch.setattr(keyboard, "send_inputs", sent.append)
    with pytest.raises(SteerError, match="gate_blocked") as info:
        press("alt+f4")
    assert info.value.code is ErrorCode.GATE_BLOCKED
    assert sent == []
    press("alt+f4", approval=GateApproval(approved_by="user", reason="test", decision_id="d1"))
    assert len(sent) == 1


def test_send_inputs_of_an_empty_batch_never_loads_user32(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise AssertionError("user32 must not be touched for an empty batch")

    monkeypatch.setattr(win_input, "_user32", boom)
    win_input.send_inputs([])


def test_send_inputs_forwards_every_record_to_one_sendinput_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(win_input, "_user32", lambda: "user32")
    monkeypatch.setattr(win_input, "_send", lambda _user32, *records: calls.append(records))
    records = text_to_inputs("ab")
    win_input.send_inputs(records)
    assert len(calls) == 1
    assert list(calls[0]) == records


def test_build_keyboard_unicode_at_the_word_boundary() -> None:
    record = win_input.build_keyboard_unicode(0xFFFF, keyup=True)
    assert record.union.ki.wVk == 0
    assert record.union.ki.wScan == 0xFFFF
    assert record.union.ki.dwFlags == 0x0004 | 0x0002
    assert record.union.ki.dwExtraInfo == STEER_EXTRA_INFO
