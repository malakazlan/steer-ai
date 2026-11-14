"""The query/commit classifier and the query outcomes (V2 §7.2, [V2.1]; probe #266: R1 only for
a searchbox or a field under a search landmark). Red on the 3c stub."""

from __future__ import annotations

import pytest

from steerai.loop2.query import SearchFacts, classify, query_outcome
from steerai.loop2.task import Commit
from steerai.world.effects import EffectClass
from tests.loop2.world_fakes import pending, report

SEARCHBOX = SearchFacts(
    searchbox=True, in_search_landmark=False, secure_in_form=False, payment_in_form=False
)
LANDMARK = SearchFacts(
    searchbox=False, in_search_landmark=True, secure_in_form=False, payment_in_form=False
)
PLAIN = SearchFacts(
    searchbox=False, in_search_landmark=False, secure_in_form=False, payment_in_form=False
)
WITH_SECURE = SearchFacts(
    searchbox=True, in_search_landmark=True, secure_in_form=True, payment_in_form=False
)
WITH_PAYMENT = SearchFacts(
    searchbox=True, in_search_landmark=True, secure_in_form=False, payment_in_form=True
)


@pending("query classifier")
@pytest.mark.parametrize("field", [SEARCHBOX, LANDMARK], ids=["searchbox", "landmark"])
def test_an_enter_on_a_search_field_with_a_goal_span_is_a_query(field: SearchFacts) -> None:
    risk = classify(verb="press", field=field, goal_span=True, commit="none", deny_word=False)
    assert risk == "R1"


@pending("query classifier")
def test_an_enter_on_a_plain_field_is_a_commit() -> None:
    risk = classify(verb="press", field=PLAIN, goal_span=True, commit="none", deny_word=False)
    assert risk == "R2"


FAILED: list[tuple[SearchFacts, bool, Commit, bool]] = [
    (WITH_SECURE, True, "none", False),
    (WITH_PAYMENT, True, "none", False),
    (SEARCHBOX, True, "none", True),
    (SEARCHBOX, False, "none", False),
    (SEARCHBOX, True, "send", False),
]


@pending("query classifier")
@pytest.mark.parametrize(
    ("field", "goal_span", "commit", "deny_word"),
    FAILED,
    ids=["secure_in_form", "payment_in_form", "deny_word", "generated_text", "commit_declared"],
)
def test_any_failed_condition_makes_it_a_commit(
    field: SearchFacts, goal_span: bool, commit: Commit, deny_word: bool
) -> None:
    risk = classify(
        verb="press", field=field, goal_span=goal_span, commit=commit, deny_word=deny_word
    )
    assert risk == "R2"


@pending("query classifier")
def test_a_button_click_in_the_fields_own_search_landmark_is_a_query() -> None:
    risk = classify(
        verb="click",
        field=LANDMARK,
        goal_span=True,
        commit="none",
        deny_word=False,
        same_landmark=True,
    )
    assert risk == "R1"


@pending("query classifier")
def test_a_button_click_outside_the_search_landmark_is_a_commit() -> None:
    risk = classify(
        verb="click",
        field=LANDMARK,
        goal_span=True,
        commit="none",
        deny_word=False,
        same_landmark=False,
    )
    assert risk == "R2"


@pending("query outcomes")
def test_a_navigation_after_a_query_is_expected() -> None:
    assert query_outcome(report("navigated"), text_carried=True) == "expected"


@pending("query outcomes")
def test_an_in_place_change_carrying_the_text_is_expected() -> None:
    assert query_outcome(report("content_changed"), text_carried=True) == "expected"


@pending("query outcomes")
def test_none_after_a_query_goes_back_to_the_controller() -> None:
    assert query_outcome(report("none"), text_carried=False) == "back_to_controller"


UNEXPECTED: list[tuple[EffectClass, bool]] = [("dialog_opened", True), ("content_changed", False)]


@pending("query outcomes")
@pytest.mark.parametrize(
    ("effect_class", "carried"), UNEXPECTED, ids=["dialog", "content_without_the_text"]
)
def test_anything_else_is_an_unexpected_effect(effect_class: EffectClass, carried: bool) -> None:
    assert query_outcome(report(effect_class), text_carried=carried) == "unexpected_effect"


@pending("query outcomes")
def test_a_window_opening_after_a_query_is_an_unexpected_effect() -> None:
    effect = report(tokens=["windows_changed"])
    assert query_outcome(effect, text_carried=True) == "unexpected_effect"
