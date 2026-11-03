"""Text sanitizer (design 5.9 [v0.8], edge cases SM-01, SM-02, SM-32, SM-33)."""

import pytest

from steerai.action.sanitize import NewlineBinding, sanitize_text
from steerai.errors import ErrorCode, SteerError


def test_plain_text_passes_unchanged() -> None:
    assert sanitize_text("hello, Amir ✓ 👍") == [("text", "hello, Amir ✓ 👍")]


@pytest.mark.parametrize("text", ["", "   ", "\t\t", chr(0xA0)])
def test_empty_or_whitespace_only_is_invalid(text: str) -> None:
    with pytest.raises(SteerError) as info:
        sanitize_text(text)
    assert info.value.code is ErrorCode.INVALID_TEXT


@pytest.mark.parametrize("bad", ["a\x1bb", "a\x03", "ctrl\x7f"])
def test_control_characters_are_invalid(bad: str) -> None:
    with pytest.raises(SteerError, match="invalid_text"):
        sanitize_text(bad)


def test_tab_is_the_only_allowed_control_character() -> None:
    assert sanitize_text("a\tb") == [("text", "a\tb")]


def test_newlines_are_rejected_without_a_binding() -> None:
    with pytest.raises(SteerError, match="newline"):
        sanitize_text("line one\nline two")


def test_newlines_become_the_app_binding_when_declared() -> None:
    parts = sanitize_text("one\r\ntwo\nthree", newline=NewlineBinding("shift+enter"))
    assert parts == [
        ("text", "one"),
        ("key", "shift+enter"),
        ("text", "two"),
        ("key", "shift+enter"),
        ("text", "three"),
    ]


def test_length_above_the_cap_is_invalid() -> None:
    with pytest.raises(SteerError, match="longer than"):
        sanitize_text("x" * 501, max_units=500)


def test_length_at_the_cap_is_allowed() -> None:
    assert sanitize_text("x" * 500, max_units=500) == [("text", "x" * 500)]


@pytest.mark.parametrize("text", ["\ud800", "ab\ud800", "\udfffx", "a\udc00b"])
def test_lone_surrogates_are_invalid_at_the_boundary(text: str) -> None:
    """A planner JSON reply can carry a lone surrogate; UTF-16 injection cannot encode it."""
    with pytest.raises(SteerError) as info:
        sanitize_text(text)
    assert info.value.code is ErrorCode.INVALID_TEXT


def test_text_exactly_at_the_cap_with_a_newline_binding_is_allowed() -> None:
    text = "a" * 498 + "\nb"
    assert len(text) == 500
    parts = sanitize_text(text, newline=NewlineBinding("shift+enter"), max_units=500)
    assert parts == [("text", "a" * 498), ("key", "shift+enter"), ("text", "b")]


def test_text_one_over_the_cap_with_a_newline_binding_is_invalid() -> None:
    text = "a" * 499 + "\nb"
    assert len(text) == 501
    with pytest.raises(SteerError, match=r"longer than the cap \(501 > 500 units\)"):
        sanitize_text(text, newline=NewlineBinding("shift+enter"), max_units=500)


def test_crlf_at_the_cap_counts_both_raw_characters() -> None:
    text = "a" * 497 + "\r\nb"
    assert len(text) == 500
    parts = sanitize_text(text, newline=NewlineBinding("shift+enter"), max_units=500)
    assert parts == [("text", "a" * 497), ("key", "shift+enter"), ("text", "b")]


def test_consecutive_newlines_emit_one_chord_each_and_no_empty_text_part() -> None:
    parts = sanitize_text("a\n\nb", newline=NewlineBinding("shift+enter"))
    assert parts == [
        ("text", "a"),
        ("key", "shift+enter"),
        ("key", "shift+enter"),
        ("text", "b"),
    ]
    assert ("text", "") not in parts


def test_leading_and_trailing_newlines_emit_chords_without_empty_text_parts() -> None:
    parts = sanitize_text("\na\n", newline=NewlineBinding("shift+enter"))
    assert parts == [("key", "shift+enter"), ("text", "a"), ("key", "shift+enter")]


def test_only_newlines_with_a_binding_is_still_whitespace_only() -> None:
    with pytest.raises(SteerError, match="empty or whitespace only"):
        sanitize_text("\n\n", newline=NewlineBinding("shift+enter"))


def test_lone_carriage_return_is_a_newline() -> None:
    with pytest.raises(SteerError, match="newline"):
        sanitize_text("a\rb")
    assert sanitize_text("a\rb", newline=NewlineBinding("shift+enter")) == [
        ("text", "a"),
        ("key", "shift+enter"),
        ("text", "b"),
    ]


@pytest.mark.parametrize("bad", ["a\x00b", "a\x0bb", "a\x0cb", "a\x1fb"])
def test_c0_and_c1_controls_other_than_tab_are_invalid(bad: str) -> None:
    with pytest.raises(SteerError, match="control or invisible characters"):
        sanitize_text(bad)


def test_cap_boundaries_zero_and_one() -> None:
    with pytest.raises(SteerError, match=r"longer than the cap \(1 > 0 units\)"):
        sanitize_text("a", max_units=0)
    assert sanitize_text("a", max_units=1) == [("text", "a")]


def test_nel_and_unicode_line_separators_are_newlines() -> None:
    # Edge case AX-32: U+0085, U+2028 and U+2029 cannot slip past the newline rule.
    for sep in (chr(0x85), chr(0x2028), chr(0x2029)):
        with pytest.raises(SteerError, match="newline"):
            sanitize_text("a" + sep + "b")


def test_bare_enter_is_not_a_valid_newline_binding() -> None:
    # Edge case AX-01: a newline binding of Enter would send the message on every line break.
    with pytest.raises(SteerError, match="bare Enter"):
        sanitize_text("a\nb", newline=NewlineBinding("enter"))


def test_newline_binding_equal_to_the_send_chord_is_refused() -> None:
    with pytest.raises(SteerError, match="send chord"):
        sanitize_text("a\nb", newline=NewlineBinding("ctrl+enter"), send_chord="ctrl+enter")


def test_bidi_controls_and_zero_width_space_are_invalid_but_emoji_joiners_pass() -> None:
    # Edge case AX-13
    for bad in (chr(0x202E), chr(0x200B), chr(0xFEFF), chr(0x2066)):
        with pytest.raises(SteerError, match="invisible"):
            sanitize_text("a" + bad + "b")
    family = "\U0001f468" + chr(0x200D) + "\U0001f469"
    assert sanitize_text(family) == [("text", family)]
    assert sanitize_text("\u2764" + chr(0xFE0F)) == [("text", "\u2764" + chr(0xFE0F))]


def test_cap_counts_utf16_units_after_normalisation() -> None:
    # Edge case AX-37: an astral character is two units; CRLF normalises to one.
    assert sanitize_text("\U0001f600", max_units=2) == [("text", "\U0001f600")]
    with pytest.raises(SteerError, match="units"):
        sanitize_text("\U0001f600", max_units=1)
