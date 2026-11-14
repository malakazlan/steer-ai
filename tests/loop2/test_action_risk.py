"""`loop.action_risk` (§7.2, §8.2, [V2.22], [V2.24] item 2): raise-only, the max of the declared
commit, v1's deny patterns on the resolved element (`gate.deny_tier_of`, no second matcher), a
secure field, text provenance at R2+, and `query.classify` for an Enter on a field or a submit
click. Each rule the lead asked to decide is one test here."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

import pytest

from steerai.loop2.loop import PendingText, action_risk
from steerai.loop2.query import SearchFacts
from steerai.world.effects import Observed, ValueFact
from steerai.world.elements import Element
from steerai.world.landmarks import Landmark
from tests.loop2.world_fakes import DOC, GOAL, PAGE, act, element, observed

TYPED = ValueFact(filled=True, digest="typed")
PLAIN = SearchFacts(
    searchbox=False, in_search_landmark=False, secure_in_form=False, payment_in_form=False
)
SEARCHY = SearchFacts(
    searchbox=True, in_search_landmark=True, secure_in_form=False, payment_in_form=False
)

FORM = element(40, "Group", "", (9, 40), 1)
BOX = element(41, "Edit", "Search", (9, 41), 40)
BOX_GO = element(42, "Button", "", (9, 42), 40)
PLAIN_BOX = element(43, "Edit", "Notes", (9, 43), 1)
TRASH = element(44, "Button", "", (9, 44), 1, automation_id="btnDelete")
PAY_GROUP = element(45, "Group", "Pay now", (9, 45), 1)
PAY_RADIO = element(46, "RadioButton", "", (9, 46), 45)
UNNAMED = element(47, "Button", "", (9, 47), 1)
SENDER = element(48, "Button", "Send", (9, 48), 1)
HISTORY = element(49, "List", "Delete history", (9, 49), 1)
SECRET = element(50, "Edit", "Password", (9, 50), 1, secure=True)
SHOWN = [
    *PAGE,
    FORM,
    BOX,
    BOX_GO,
    PLAIN_BOX,
    TRASH,
    PAY_GROUP,
    PAY_RADIO,
    UNNAMED,
    SENDER,
    HISTORY,
    SECRET,
]
IN_SEARCH = Landmark(kind="search", runtime_id=FORM.runtime_id)


def screen() -> Observed:
    marks: dict[tuple[int, ...], Landmark | None] = {e.runtime_id: None for e in SHOWN}
    marks |= {BOX.runtime_id: IN_SEARCH, BOX_GO.runtime_id: IN_SEARCH}
    return dataclasses.replace(observed(SHOWN), landmarks=marks)


def pending(field: Element, search: SearchFacts) -> list[PendingText]:
    return [PendingText(field.runtime_id, TYPED, TYPED, search, goal_span=True)]


def risk(
    verb: str,
    target: Element,
    *,
    pend: Sequence[PendingText] = (),
    search: SearchFacts | None = None,
    **kw: object,
) -> str:
    action = act(verb, **({"target": target.id} if verb not in {"press", "wait"} else {}), **kw)
    return action_risk(action, target, goal=GOAL, search=search, observed=screen(), pending=pend)


# ---- a press takes the deny scoping of the element it goes to ---------------------------------


def test_a_press_on_a_deny_word_element_is_raised_by_that_element() -> None:
    assert risk("press", SENDER, key="Tab") == "R2"


def test_a_press_on_a_neutral_element_is_not_raised() -> None:
    assert risk("press", UNNAMED, key="Tab") == "R0"


# ---- an Enter on a field is classified like a query or a commit --------------------------------


def test_an_enter_on_a_searchbox_holding_a_goal_span_is_a_query() -> None:
    assert risk("press", BOX, key="Enter", pend=pending(BOX, SEARCHY), search=SEARCHY) == "R1"


def test_an_enter_on_a_plain_field_is_a_commit() -> None:
    assert (
        risk("press", PLAIN_BOX, key="Enter", pend=pending(PLAIN_BOX, PLAIN), search=PLAIN) == "R2"
    )


def test_an_enter_on_a_search_field_with_no_typed_goal_span_is_a_commit() -> None:
    assert risk("press", BOX, key="Enter", search=SEARCHY) == "R2"


def test_an_enter_with_no_search_facts_fails_closed_to_a_commit() -> None:
    assert risk("press", BOX, key="Enter", pend=pending(BOX, SEARCHY), search=None) == "R2"


# ---- an unnamed target: its automation id, its group name, the pending-text rule ---------------


def test_an_unnamed_target_is_raised_by_its_automation_id() -> None:
    assert risk("click", TRASH) == "R3"


def test_an_unnamed_radio_is_raised_by_its_group_name() -> None:
    assert risk("click", PAY_RADIO) == "R3"


def test_an_unnamed_target_with_nothing_to_read_stays_r0() -> None:
    assert risk("click", UNNAMED) == "R0"


def test_an_unnamed_button_in_the_search_form_with_pending_text_is_a_query() -> None:
    assert risk("click", BOX_GO, pend=pending(BOX, SEARCHY)) == "R1"


# ---- scroll and wait --------------------------------------------------------------------------


def test_a_scroll_never_reads_deny_words() -> None:
    assert risk("scroll", HISTORY) == "R0"


def test_a_wait_is_r0() -> None:
    assert risk("wait", DOC) == "R0"


@pytest.mark.parametrize("verb", ["scroll", "wait"])
def test_a_declared_commit_still_raises_a_scroll_or_a_wait(verb: str) -> None:
    target = HISTORY if verb == "scroll" else DOC
    assert risk(verb, target, commit="pay") == "R3"


# ---- the max with the declared commit ---------------------------------------------------------


@pytest.mark.parametrize("commit", ["pay", "delete"])
def test_classify_r2_for_a_pay_or_delete_commit_is_maxed_to_the_declared_r3(commit: str) -> None:
    raised = risk(
        "press", BOX, key="Enter", pend=pending(BOX, SEARCHY), search=SEARCHY, commit=commit
    )
    assert raised == "R3"


def test_a_declared_send_commit_is_r2() -> None:
    assert risk("click", UNNAMED, commit="send") == "R2"


# ---- a secure field, and generated text at R2+ ------------------------------------------------


def test_typing_into_a_secure_field_is_r3() -> None:
    assert risk("type", SECRET, param="text") == "R3"


def test_generated_text_at_r2_is_raised_to_r3() -> None:
    assert risk("type", PLAIN_BOX, text="hello", commit="send") == "R3"


def test_text_by_param_at_r2_stays_r2() -> None:
    assert risk("type", PLAIN_BOX, param="text", commit="send") == "R2"


def test_generated_text_below_r2_is_not_raised() -> None:
    assert risk("type", PLAIN_BOX, text="hello") == "R0"


# ---- the submit rule fails closed on missing facts --------------------------------------------


def test_pending_text_with_no_observation_fails_closed() -> None:
    action = act("click", target=UNNAMED.id)
    pend = pending(PLAIN_BOX, PLAIN)
    assert action_risk(action, UNNAMED, goal=GOAL, search=None, observed=None, pending=pend) == "R2"


def test_pending_text_with_an_unread_landmark_map_fails_closed() -> None:
    blind = dataclasses.replace(screen(), landmarks=None)
    action = act("click", target=UNNAMED.id)
    pend = pending(BOX, SEARCHY)
    assert (
        action_risk(action, UNNAMED, goal=GOAL, search=None, observed=blind, pending=pend) == "R2"
    )
