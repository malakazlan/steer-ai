"""An open-only subgoal holds on the open's code-owned title (issue #47, packet open-subgoal,
schema gate REVISE 7): every name pattern of the condition is contained in the title the open
verified, folded, counter stripped; a condition the window contradicts never holds."""

from __future__ import annotations

import pytest

from steerai.action.launch import expected_title, open_condition_holds, open_title_judges
from steerai.verify.predicates import (
    All,
    AnyOf,
    Appeared,
    CountDelta,
    Disappeared,
    Predicate,
    TitleMatches,
    WindowExists,
    WindowGone,
)

NOTEPAD = "Untitled - Notepad"
RESULTS = expected_title("chrome", "youtube_search", "drake latest video")


def test_run_57s_conditions_hold_on_the_opened_windows() -> None:
    """Live PR #46 run 57: both conditions never held in a fresh window under the old rule."""
    assert open_condition_holds(WindowExists(name_pattern="Notepad"), NOTEPAD)
    replacing = Appeared(
        role="Document", name_pattern="drake latest video", decoration="youtube", replaces=True
    )
    assert open_condition_holds(replacing, RESULTS)


def test_a_condition_the_window_contradicts_never_holds() -> None:
    """Gate Q2: an open searching "drake" is not "drake latest video"."""
    narrower = expected_title("chrome", "youtube_search", "drake")
    assert not open_condition_holds(TitleMatches(pattern="drake latest video"), narrower)
    assert not open_condition_holds(TitleMatches(pattern="Google Search"), RESULTS)


def test_matching_folds_case_and_whitespace_and_strips_a_counter() -> None:
    assert open_condition_holds(TitleMatches(pattern="DRAKE   latest video"), RESULTS)
    assert open_condition_holds(TitleMatches(pattern="(3) drake latest video"), RESULTS)


def test_all_needs_every_part_and_any_of_one() -> None:
    good, bad = TitleMatches(pattern="drake"), TitleMatches(pattern="nope")
    assert open_condition_holds(All(parts=(good, good)), RESULTS)
    assert not open_condition_holds(All(parts=(good, bad)), RESULTS)
    assert open_condition_holds(AnyOf(parts=(bad, good)), RESULTS)
    assert not open_condition_holds(AnyOf(parts=(bad, bad)), RESULTS)


@pytest.mark.parametrize(
    "condition", [CountDelta(role="ListItem", delta=1), Appeared(role="Text", name_pattern="drake")]
)
def test_other_kinds_are_not_judged_by_the_title(condition: Predicate) -> None:
    """A non-replacing appeared or a count is about the page's content, not its title."""
    assert not open_condition_holds(condition, RESULTS)
    assert not open_title_judges(condition)


def test_the_title_judges_a_tree_only_when_it_speaks_for_every_part() -> None:
    """Gate ruling R3 (hunter OS-05): a mix, or a removal kind, keeps the live check."""
    title = TitleMatches(pattern="drake")
    assert open_title_judges(All(parts=(title, WindowExists(name_pattern=NOTEPAD))))
    assert open_title_judges(
        AnyOf(parts=(title, Appeared(role="Document", name_pattern="x", replaces=True)))
    )
    assert not open_title_judges(All(parts=(title, CountDelta(role="ListItem", delta=1))))
    assert not open_title_judges(WindowGone(name_pattern="Notepad"))
    assert not open_title_judges(Disappeared(role="Document", name_pattern="drake"))


def test_a_pattern_must_be_whole_words_of_the_title() -> None:
    """Issue #53: a substring like "e" or "Note" is in almost any title and proves nothing."""
    assert not open_condition_holds(TitleMatches(pattern="e"), NOTEPAD)
    assert not open_condition_holds(WindowExists(name_pattern="Note"), NOTEPAD)
    assert not open_condition_holds(TitleMatches(pattern="latest vid"), RESULTS)
    assert open_condition_holds(TitleMatches(pattern="latest video"), RESULTS)
    assert open_condition_holds(WindowExists(name_pattern="Notepad"), NOTEPAD)


def test_the_words_must_be_in_the_titles_order() -> None:
    assert not open_condition_holds(TitleMatches(pattern="video latest"), RESULTS)


def test_matching_uses_the_shared_comparable_form() -> None:
    """Issue #53: NFKC, as every other name match (an invisible character is refused when the
    predicate is built)."""
    fullwidth = "".join(chr(0xFEE0 + ord(c)) for c in "Notepad")  # fullwidth letters
    assert open_condition_holds(WindowExists(name_pattern=fullwidth), NOTEPAD)


def test_punctuation_alone_proves_nothing() -> None:
    """Principal review of #53: every registry title holds " - "."""
    assert not open_condition_holds(TitleMatches(pattern="-"), RESULTS)
    assert not open_condition_holds(TitleMatches(pattern="- :"), NOTEPAD)


def test_punctuation_at_a_words_edge_is_not_part_of_the_word() -> None:
    """A title like "drake, kendrick - YouTube" still shows the word "drake"."""
    title = "drake, kendrick - YouTube - Google Chrome"
    assert open_condition_holds(TitleMatches(pattern="drake"), title)
    assert open_condition_holds(TitleMatches(pattern="drake kendrick"), title)
    assert not open_condition_holds(TitleMatches(pattern="drak"), title)
