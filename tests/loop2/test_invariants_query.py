"""The query/commit classifier and the query outcomes (V2 §7.2, [V2.1]; probe #266: R1 only for
a searchbox or a field under a search landmark). Red on the 3c stub."""

from __future__ import annotations

import pytest

from steerai.loop2.query import FieldState, SearchFacts, classify, query_outcome
from steerai.loop2.task import Commit
from steerai.world.effects import EffectClass, EffectReport, Token
from tests.loop2.world_fakes import report

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


@pytest.mark.parametrize("field", [SEARCHBOX, LANDMARK], ids=["searchbox", "landmark"])
def test_an_enter_on_a_search_field_with_a_goal_span_is_a_query(field: SearchFacts) -> None:
    risk = classify(verb="press", field=field, goal_span=True, commit="none", deny_word=False)
    assert risk == "R1"


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


# ---- [V2.21] #24, [V2.22] #25: outcomes after a query or caution Enter or click ---------------
# The nine rows, in order; the first match wins.


def outcome(
    effect: EffectReport,
    *,
    carried: bool = True,
    field: FieldState = "not_typed",
    top_replaced: bool | None = None,
) -> tuple[str, str, int]:
    seen = query_outcome(effect, text_carried=carried, typed_field=field, top_replaced=top_replaced)
    return seen.outcome, seen.token, seen.row


def test_row_1_a_window_change_stops() -> None:
    effect = report(tokens=["windows_changed"])
    assert outcome(effect) == ("unexpected_effect", "unexpected_effect", 1)


def test_a_window_opening_after_a_query_is_an_unexpected_effect() -> None:
    effect = report(tokens=["windows_changed"])
    assert query_outcome(effect, text_carried=True).outcome == "unexpected_effect"


def test_row_2_the_typed_field_emptied_stops() -> None:
    effect = report("own_value", "content_changed")
    assert outcome(effect, field="emptied") == ("unexpected_effect", "unexpected_effect", 2)


def test_row_2_an_unread_field_is_unknown_never_assumed_kept() -> None:
    effect = report("content_changed")
    assert outcome(effect, field="unread") == ("back_to_controller", "unknown", 2)


@pytest.mark.parametrize("field", ["not_typed", "kept", "gone"])
def test_row_2_is_skipped_when_the_field_was_not_emptied(field: FieldState) -> None:
    assert outcome(report("content_changed"), field=field)[2] != 2


def test_row_3_a_reload_of_the_top_document_stops() -> None:
    effect = report(tokens=["rerendered"])
    assert outcome(effect, top_replaced=True) == ("unexpected_effect", "unexpected_effect", 3)


def test_row_3_a_rerender_with_the_top_document_unread_is_unknown() -> None:
    effect = report(tokens=["rerendered"])
    assert outcome(effect, top_replaced=None) == ("back_to_controller", "unknown", 3)


def test_an_element_level_rerender_is_weak() -> None:
    effect = report(tokens=["rerendered"])
    assert outcome(effect, top_replaced=False) == ("back_to_controller", "weak", 8)


@pytest.mark.parametrize("effect_class", ["dialog_opened", "revealed", "content_changed"])
def test_row_4_a_change_with_no_carrier_stops(effect_class: EffectClass) -> None:
    effect = report(effect_class)
    assert outcome(effect, carried=False) == ("unexpected_effect", "unexpected_effect", 4)


def test_row_5_unknown_goes_back_as_unknown() -> None:
    assert outcome(report("unknown")) == ("back_to_controller", "unknown", 5)


def test_row_5_no_class_and_an_unread_fact_is_unknown() -> None:
    assert outcome(report(unread=["url"])) == ("back_to_controller", "unknown", 5)


@pytest.mark.parametrize(
    "effect_class", ["navigated", "url_changed_in_place", "content_changed", "revealed"]
)
def test_row_6_a_strong_class_with_a_carrier_is_expected(effect_class: EffectClass) -> None:
    assert outcome(report(effect_class)) == ("expected", "none", 6)


def test_row_7_a_navigation_with_no_carrier_goes_back() -> None:
    assert outcome(report("navigated"), carried=False) == ("back_to_controller", "no_carrier", 7)


WEAK: list[tuple[tuple[EffectClass, ...], tuple[Token, ...]]] = [
    (("focus",), ()),
    (("own_state",), ()),
    (("own_value",), ()),
    ((), ("frame_changed",)),
    ((), ("title_only",)),
    ((), ("dismissed",)),
    ((), ("scrolled",)),
    (("none",), ("moved",)),
    (("focus", "own_state"), ("frame_changed", "title_only")),
]


@pytest.mark.parametrize(("classes", "tokens"), WEAK)
def test_row_8_weak_only_goes_back_as_weak(
    classes: tuple[EffectClass, ...], tokens: tuple[Token, ...]
) -> None:
    effect = report(*classes, tokens=tokens)
    assert outcome(effect, field="kept") == ("back_to_controller", "weak", 8)


def test_dismissed_is_weak_not_a_stop() -> None:
    """[V2.21]: Enter closing a suggestion list is routine."""
    assert outcome(report(tokens=["dismissed"]))[1] == "weak"


def test_own_value_is_weak_only_while_the_field_is_still_filled() -> None:
    assert outcome(report("own_value"), field="kept")[1] == "weak"
    assert outcome(report("own_value"), field="emptied")[2] == 2


def test_row_9_proven_none_goes_back_as_none() -> None:
    assert outcome(report("none")) == ("back_to_controller", "none", 9)


# ---- precedence and the caution key ---------------------------------------------------------


def test_a_window_change_beats_a_strong_class_with_a_carrier() -> None:
    effect = report("navigated", tokens=["windows_changed"])
    assert outcome(effect)[2] == 1


def test_a_field_emptied_beats_content_with_a_carrier() -> None:
    assert outcome(report("content_changed"), field="emptied")[2] == 2


def test_a_dialog_beside_a_strong_class_with_a_carrier_is_expected() -> None:
    assert outcome(report("dialog_opened", "content_changed")) == ("expected", "none", 6)


@pytest.mark.parametrize(
    ("effect", "carried", "released"),
    [
        (report("none"), True, True),
        (report("unknown"), True, False),
        (report("navigated"), False, False),
        (report("focus"), True, False),
        (report("navigated"), True, False),
    ],
    ids=["row9_none", "row5_unknown", "row7_no_carrier", "row8_weak", "row6_expected"],
)
def test_the_caution_key_is_released_only_on_row_9(
    effect: EffectReport, carried: bool, released: bool
) -> None:
    assert query_outcome(effect, text_carried=carried).releases_key is released


# ---- [V2.22] #25 a query click takes the same table -------------------------------------------


def test_a_query_click_with_no_typed_field_skips_row_2() -> None:
    """The field was already filled: no field typed under the subgoal shares the button's
    search landmark, so row 2 is skipped, not failed."""
    assert outcome(report("content_changed"), field="not_typed") == ("expected", "none", 6)


@pytest.mark.parametrize("effect_class", ["own_state", "focus"])
def test_the_buttons_own_change_or_focus_moving_to_it_is_weak(effect_class: EffectClass) -> None:
    assert outcome(report(effect_class)) == ("back_to_controller", "weak", 8)


# ---- [V2.23] A: the table's unmatched inputs ---------------------------------------------------


def test_a_dialog_with_no_strong_class_stops_whatever_the_carrier() -> None:
    assert outcome(report("dialog_opened"), carried=True)[:2] == (
        "unexpected_effect",
        "unexpected_effect",
    )
    assert outcome(report("dialog_opened"), carried=True)[2] == 4


def test_a_dialog_inside_the_scope_with_revealed_and_a_carrier_is_expected() -> None:
    """A search overlay showing results: the observer reports an in-scope dialog with
    `revealed`."""
    assert outcome(report("dialog_opened", "revealed")) == ("expected", "none", 6)


def test_a_dialog_outside_the_scope_stops_even_with_a_url_change() -> None:
    """An out-of-scope dialog comes with `frame_changed`, never `revealed`."""
    effect = report("dialog_opened", "url_changed_in_place", tokens=["frame_changed"])
    assert outcome(effect, carried=True) == ("unexpected_effect", "unexpected_effect", 4)


def test_a_url_change_in_place_without_a_carrier_is_no_carrier() -> None:
    effect = report("url_changed_in_place")
    assert outcome(effect, carried=False) == ("back_to_controller", "no_carrier", 7)


def test_a_url_change_in_place_beats_content_without_a_carrier() -> None:
    """Row 4's no-carrier stop applies only when the URL did not change."""
    effect = report("url_changed_in_place", "content_changed", "revealed")
    assert outcome(effect, carried=False) == ("back_to_controller", "no_carrier", 7)


def test_content_without_a_carrier_and_no_url_change_still_stops() -> None:
    assert outcome(report("content_changed"), carried=False)[2] == 4


def test_own_value_with_no_typed_field_is_weak() -> None:
    assert outcome(report("own_value"), field="not_typed") == ("back_to_controller", "weak", 8)
