"""Predicate language (design 5.10 [v0.2]): typed, plain-text patterns, validated at construction,
secrets never in the repr."""

from __future__ import annotations

import pytest

from steerai.verify.predicates import (
    All,
    AnyOf,
    Appeared,
    CountDelta,
    Disappeared,
    Focused,
    State,
    TitleMatches,
    ValueEquals,
    WindowExists,
    Within,
    name_matches,
)


def test_patterns_are_plain_text_normalised_once() -> None:
    assert Appeared(role="Text", name_pattern="  hello\u00a0world ").name_pattern == "hello world"


@pytest.mark.parametrize("pattern", ["", "   ", "\u200b", "a\u202eb", "x\x07"])
def test_empty_or_invisible_patterns_are_rejected(pattern: str) -> None:
    with pytest.raises(ValueError, match="pattern"):
        Appeared(role="Text", name_pattern=pattern)


def test_roles_must_be_named() -> None:
    with pytest.raises(ValueError, match="role"):
        Disappeared(role="", name_pattern="x")


def test_count_delta_must_be_non_zero() -> None:
    with pytest.raises(ValueError, match="delta"):
        CountDelta(role="ListItem", delta=0)


def test_state_accepts_only_the_three_states() -> None:
    with pytest.raises(ValueError, match="state"):
        State(target_id=1, wanted="half")  # type: ignore[arg-type]


def test_conjunctions_need_at_least_two_parts() -> None:
    with pytest.raises(ValueError, match="parts"):
        All((Focused(target_id=1),))
    with pytest.raises(ValueError, match="parts"):
        AnyOf((Focused(target_id=1),))


def test_patterns_and_values_are_out_of_the_repr() -> None:
    assert "secret-text" not in repr(ValueEquals(target_id=1, text="secret-text"))
    assert "hunter2" not in repr(Appeared(role="Text", name_pattern="hunter2"))
    assert "Mom" not in repr(TitleMatches(pattern="Mom - Chat"))
    assert "Mom" not in repr(WindowExists(name_pattern="Mom"))


def test_within_is_a_landmark_or_an_anchor_never_both() -> None:
    with pytest.raises(ValueError, match="Within"):
        Within(landmark="Conversation", anchor_id=3)
    assert Within(landmark="Conversation").landmark == "conversation"


def test_window_wide_match_is_anchored_and_scoped_match_is_a_substring() -> None:
    assert name_matches("hello world", "hello world", scoped=False) is True
    assert name_matches("hello world!", "hello world", scoped=False) is False
    assert name_matches("Hello World", "hello world", scoped=False) is False
    assert name_matches("Sara: hello world!", "hello world", scoped=True) is True
    assert name_matches("Sara: Hello World", "hello world", scoped=True) is True


def test_match_normalises_compatibility_forms_and_whitespace() -> None:
    assert name_matches("hello\u00a0 world", "hello world", scoped=False) is True
    assert name_matches("\uff28ello", "Hello", scoped=False) is True
