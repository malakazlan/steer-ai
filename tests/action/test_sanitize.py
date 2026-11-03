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
        sanitize_text("x" * 501, max_chars=500)


def test_length_at_the_cap_is_allowed() -> None:
    assert sanitize_text("x" * 500, max_chars=500) == [("text", "x" * 500)]
