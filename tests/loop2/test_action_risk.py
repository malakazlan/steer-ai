"""`loop.action_risk` (§7.2, §8.2, [V2.22], [V2.24] item 2, [V2.25]): raise-only, the max of the
declared commit, v1's deny patterns on the resolved element (`gate.deny_tier_of`, no second
matcher), a secure field, and `query.classify` for an Enter on a field or a submit of pending
text; with ask reasons beside the tier (`generated_text`, which no grant covers, and
`submit_pending_text`). Each rule the lead asked to decide is one test here."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

import pytest

from steerai.loop2.actuate import press_allowed
from steerai.loop2.loop import PendingText, RiskVerdict, action_risk
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
WRAP = element(51, "Group", "", (9, 51), 1)
COMPOSER = element(52, "Edit", "Message", (9, 52), 51)
ICON = element(53, "Button", "", (9, 53), 51)
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
    WRAP,
    COMPOSER,
    ICON,
]
IN_SEARCH = Landmark(kind="search", runtime_id=FORM.runtime_id)


def screen(shown: Sequence[Element] = SHOWN) -> Observed:
    marks: dict[tuple[int, ...], Landmark | None] = {e.runtime_id: None for e in shown}
    marks |= {BOX.runtime_id: IN_SEARCH, BOX_GO.runtime_id: IN_SEARCH}
    return dataclasses.replace(observed(shown), landmarks=marks)


def pending(
    field: Element,
    search: SearchFacts = PLAIN,
    *,
    goal_span: bool = True,
    now: ValueFact | None = TYPED,
) -> list[PendingText]:
    return [PendingText(field.runtime_id, TYPED, now, search, goal_span=goal_span)]


def verdict(
    verb: str,
    target: Element,
    *,
    pend: Sequence[PendingText] = (),
    seen: Observed | None = None,
    **kw: object,
) -> RiskVerdict:
    targeted = {"target": target.id} if verb not in {"press", "wait"} else {}
    action = act(verb, **targeted, **kw)
    return action_risk(action, target, goal=GOAL, observed=seen or screen(), pending=pend)


def risk(verb: str, target: Element, **kw: object) -> str:
    return verdict(verb, target, **kw).risk  # type: ignore[arg-type]  # test kwargs


# ---- a press takes the deny scoping of the element it goes to ---------------------------------


def test_a_press_on_a_deny_word_element_is_raised_by_that_element() -> None:
    assert risk("press", SENDER, key="Tab") == "R2"


def test_a_press_on_a_neutral_element_is_not_raised() -> None:
    assert risk("press", UNNAMED, key="Tab") == "R0"


def test_a_press_with_focus_unread_is_refused_before_any_risk() -> None:
    """[V2.6]: the press target is the last acted element, and an unread focus refuses it."""
    assert press_allowed(focus=None, last_acted=UNNAMED.runtime_id) is False


# ---- an Enter on a field is classified like a query or a commit --------------------------------


def test_an_enter_on_a_searchbox_holding_a_goal_span_is_a_query() -> None:
    assert risk("press", BOX, key="Enter", pend=pending(BOX, SEARCHY)) == "R1"


def test_an_enter_on_a_plain_field_is_a_commit() -> None:
    assert risk("press", PLAIN_BOX, key="Enter", pend=pending(PLAIN_BOX)) == "R2"


def test_an_enter_on_a_search_field_with_no_typed_text_is_a_commit() -> None:
    assert risk("press", BOX, key="Enter") == "R2"


def test_an_enter_on_a_field_whose_value_is_unread_fails_closed() -> None:
    assert risk("press", BOX, key="Enter", pend=pending(BOX, SEARCHY, now=None)) == "R2"


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
    raised = risk("press", BOX, key="Enter", pend=pending(BOX, SEARCHY), commit=commit)
    assert raised == "R3"


def test_a_declared_send_commit_is_r2() -> None:
    assert risk("click", UNNAMED, commit="send") == "R2"


# ---- a secure field ---------------------------------------------------------------------------


def test_typing_into_a_secure_field_is_r3() -> None:
    assert risk("type", SECRET, param="text") == "R3"


# ---- [V2.25] (a): generated text keeps its tier and adds an ask no grant covers ---------------


def test_generated_text_at_r2_keeps_r2_and_asks_generated_text() -> None:
    seen = verdict("type", PLAIN_BOX, text="hello", commit="send")
    assert (seen.risk, seen.reasons) == ("R2", frozenset({"generated_text"}))


def test_text_by_param_at_r2_has_no_generated_text_reason() -> None:
    seen = verdict("type", PLAIN_BOX, param="text", commit="send")
    assert (seen.risk, seen.reasons) == ("R2", frozenset())


def test_generated_text_below_r2_changes_nothing() -> None:
    seen = verdict("type", PLAIN_BOX, text="hello")
    assert (seen.risk, seen.reasons) == ("R0", frozenset())


def test_an_enter_on_a_field_holding_generated_text_is_never_a_query() -> None:
    seen = verdict("press", BOX, key="Enter", pend=pending(BOX, SEARCHY, goal_span=False))
    assert seen.risk == "R2"
    assert "generated_text" in seen.reasons


def test_the_icon_click_sending_generated_text_asks_generated_text() -> None:
    """Principal 5: a send of generated pending text keeps R2 and carries the reason."""
    seen = verdict("click", ICON, pend=pending(COMPOSER, goal_span=False))
    assert seen.risk == "R2"
    assert {"generated_text", "submit_pending_text"} <= seen.reasons


def test_any_action_at_r2_while_generated_text_is_unread_asks_generated_text() -> None:
    seen = verdict("click", SENDER, pend=pending(COMPOSER, goal_span=False, now=None))
    assert seen.risk == "R2"
    assert "generated_text" in seen.reasons


def test_an_r0_action_while_generated_text_is_pending_adds_nothing() -> None:
    seen = verdict("click", UNNAMED, pend=pending(COMPOSER, goal_span=False))
    assert (seen.risk, seen.reasons) == ("R0", frozenset())


# ---- [V2.25] (b): the submit rule's blocks are counted by the field's parent kind ------------


def test_a_submit_block_under_a_narrow_container_records_narrower() -> None:
    seen = verdict("click", ICON, pend=pending(COMPOSER))
    assert seen.risk == "R2"
    assert "submit_pending_text" in seen.reasons
    assert seen.submit_parent == "narrower"


def test_a_submit_block_with_the_field_directly_under_the_document_records_document() -> None:
    loose = element(54, "Edit", "Message", (9, 54), DOC.id)
    seen = verdict("click", UNNAMED, pend=pending(loose), seen=screen([*SHOWN, loose]))
    assert seen.risk == "R2", "no Document exception: it would reopen the hole"
    assert seen.submit_parent == "document"


def test_a_submit_block_with_the_field_directly_under_main_records_main() -> None:
    main = element(55, "Group", "", (9, 55), DOC.id)
    composer = element(56, "Edit", "Message", (9, 56), 55)
    icon = element(57, "Button", "", (9, 57), 55)
    shown = [*SHOWN, main, composer, icon]
    in_main = Landmark(kind="main", runtime_id=main.runtime_id)
    marks: dict[tuple[int, ...], Landmark | None] = {e.runtime_id: None for e in shown}
    marks |= {composer.runtime_id: in_main, icon.runtime_id: in_main}
    seen_main = dataclasses.replace(observed(shown), landmarks=marks)
    seen = verdict("click", icon, pend=pending(composer), seen=seen_main)
    assert seen.risk == "R2"
    assert seen.submit_parent == "main"


def test_a_plain_click_has_no_submit_parent() -> None:
    assert verdict("click", UNNAMED).submit_parent is None


# ---- principal review: every verb that can activate an element, and missing facts ------------


@pytest.mark.parametrize("verb", ["focus", "expand", "collapse", "check", "uncheck", "select"])
def test_the_submit_rule_applies_to_every_activating_verb(verb: str) -> None:
    assert risk(verb, ICON, pend=pending(COMPOSER)) == "R2"


def test_pending_text_with_an_unread_landmark_map_fails_closed() -> None:
    blind = dataclasses.replace(screen(), landmarks=None)
    assert risk("click", UNNAMED, pend=pending(BOX, SEARCHY), seen=blind) == "R2"


def test_a_broken_parent_chain_fails_closed() -> None:
    """Principal 3: a missing parent between the target and the field's container is not
    "elsewhere"; it is unknown, so R2."""
    deep = element(58, "Button", "", (9, 58), 59)
    shown = [e for e in SHOWN if e.id != ICON.id] + [deep]
    seen = screen(shown)
    assert risk("click", deep, pend=pending(COMPOSER), seen=dataclasses.replace(seen)) == "R2"


# ---- [V2.25] addendum: a changed digest ---------------------------------------------------------

CHANGED = ValueFact(filled=True, digest="reformatted")
EMPTY = ValueFact(filled=False, digest="empty")


def test_same_digest_keeps_the_rule_on_and_a_query_possible() -> None:
    seen = verdict("click", BOX_GO, pend=pending(BOX, SEARCHY))
    assert (seen.risk, seen.reasons, seen.refusal) == ("R1", frozenset(), None)


def test_a_changed_digest_still_filled_keeps_the_rule_on_never_a_query_and_asks() -> None:
    seen = verdict("click", BOX_GO, pend=pending(BOX, SEARCHY, now=CHANGED))
    assert seen.risk == "R2", "never a query: the field no longer provably holds a goal span"
    assert "text_changed" in seen.reasons
    assert seen.refusal is None


def test_a_changed_digest_under_a_declared_send_is_refused() -> None:
    seen = verdict("click", ICON, pend=pending(COMPOSER, now=CHANGED), commit="send")
    assert seen.refusal == "text_changed"


def test_an_enter_on_a_changed_field_under_a_declared_submit_is_refused() -> None:
    seen = verdict(
        "press", BOX, key="Enter", pend=pending(BOX, SEARCHY, now=CHANGED), commit="submit"
    )
    assert seen.refusal == "text_changed"


def test_an_emptied_field_turns_the_rule_off() -> None:
    seen = verdict("click", ICON, pend=pending(COMPOSER, now=EMPTY))
    assert (seen.risk, seen.reasons, seen.submit_parent) == ("R0", frozenset(), None)


def test_a_target_whose_parent_is_not_in_the_snapshot_fails_closed() -> None:
    """[V2.25] addendum 6: the group name cannot be read, so it is not read as "no group"."""
    orphan = element(60, "RadioButton", "", (9, 60), 61)
    assert risk("click", orphan, seen=screen([*SHOWN, orphan])) == "R2"
