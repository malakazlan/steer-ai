"""The v2 action key and write-ahead record ([V2.15] Q1, Q9; V2 §8.4). The key is v1's scheme
and never holds the subgoal; the record does. Red on the step 4 stub. The v1 key values are
pinned, passing, in `test_ledger_keys.py`."""

from __future__ import annotations

import pytest

from steerai.loop2.record import V2Record, key_for, open_record, start_refusal
from steerai.orchestrator.contracts import GateDecision, GateOutcome, Target
from steerai.orchestrator.ledger import action_key
from tests.loop2.world_fakes import FIELD, PAGE, SEND, act, pending, snapshot

SECRET = bytes(range(32))
BEFORE = snapshot(PAGE)


@pending("[V2.15] Q1: the subgoal is never in the key")
def test_the_same_action_for_two_subgoals_has_one_key() -> None:
    one = key_for(SECRET, act("click", target=SEND.id, subgoal="s1"), SEND, text=None)
    two = key_for(SECRET, act("click", target=SEND.id, subgoal="s2"), SEND, text=None)
    assert one == two


@pending("[V2.15] Q1: the key stays verb + target identity + params")
def test_a_click_key_is_v1s_action_key() -> None:
    expected = action_key(SECRET, "click", Target(role="Button", name="Send"), {})
    assert key_for(SECRET, act("click", target=SEND.id), SEND, text=None) == expected


@pending("[V2.15] Q1: the key stays verb + target identity + params")
def test_a_type_key_carries_its_text_as_v1_does() -> None:
    expected = action_key(SECRET, "type", Target(role="Edit", name="Search"), {"text": "red shoes"})
    typed = act("type", target=FIELD.id, param="text")
    assert key_for(SECRET, typed, FIELD, text="red shoes") == expected


@pending("[V2.15] Q1: the key stays verb + target identity + params")
def test_a_press_key_carries_its_key_param() -> None:
    expected = action_key(SECRET, "press", Target(role="Edit", name="Search"), {"key": "Enter"})
    assert key_for(SECRET, act("press", key="Enter"), FIELD, text=None) == expected


@pending("[V2.15] Q1: other params are another action")
def test_other_text_is_another_key() -> None:
    typed = act("type", target=FIELD.id, param="text")
    assert key_for(SECRET, typed, FIELD, text="red shoes") != key_for(
        SECRET, typed, FIELD, text="blue shoes"
    )


def decision(key: str = "k-1", outcome: GateOutcome = "confirm") -> GateDecision:
    return GateDecision(decision_id="d-000000000001", action_key=key, outcome=outcome)


@pending("[V2.15] Q1: the subgoal id is stored in the record, to bind the proof")
def test_the_record_holds_the_subgoal_and_the_key() -> None:
    record = open_record(
        action_key="k-1",
        subgoal="s1",
        verb="click",
        role="Button",
        before=BEFORE,
        decision=decision(),
    )
    assert (record.subgoal, record.action_key, record.phase) == ("s1", "k-1", "written")
    assert record.decision_id == "d-000000000001"


@pending("§8.4: the write-ahead record consumes the decision bound to its key")
def test_opening_a_record_consumes_the_decision() -> None:
    granted = decision()
    open_record(
        action_key="k-1", subgoal="s1", verb="click", role="Button", before=BEFORE, decision=granted
    )
    assert granted.consumed


@pending("§8.4: a decision for another key never opens a record")
def test_a_decision_for_another_key_is_refused() -> None:
    with pytest.raises(ValueError, match="decision"):
        open_record(
            action_key="k-1",
            subgoal="s1",
            verb="click",
            role="Button",
            before=BEFORE,
            decision=decision("k-2"),
        )


@pending("§8.4: one decision opens one record")
def test_a_consumed_decision_never_opens_a_second_record() -> None:
    granted = decision()
    open_record(
        action_key="k-1", subgoal="s1", verb="click", role="Button", before=BEFORE, decision=granted
    )
    with pytest.raises(ValueError, match="decision"):
        open_record(
            action_key="k-1",
            subgoal="s1",
            verb="click",
            role="Button",
            before=BEFORE,
            decision=granted,
        )


@pending("§8.4: a block decision never opens a record")
def test_a_block_decision_is_refused() -> None:
    with pytest.raises(ValueError, match="decision"):
        open_record(
            action_key="k-1",
            subgoal="s1",
            verb="click",
            role="Button",
            before=BEFORE,
            decision=decision(outcome="block"),
        )


def record(key: str, verb: str = "click", role: str = "Button") -> V2Record:
    return V2Record(
        action_key=key,
        subgoal="s1",
        decision_id="d-000000000001",
        verb=verb,
        role=role,
        phase="acting",
        before=BEFORE,
    )


@pending("[V2.15] Q9: no resume; an unsettled record refuses the start and names the action")
def test_an_open_record_refuses_the_start_and_names_the_action() -> None:
    refusal = start_refusal([record("k-1", "press", "Edit")], {"k-1": None})
    assert refusal is not None
    assert (refusal.token, refusal.verb, refusal.role) == ("unsettled_action", "press", "Edit")


@pending("[V2.15] Q9: an unknown outcome is unsettled")
def test_an_unknown_outcome_refuses_the_start() -> None:
    assert start_refusal([record("k-1")], {"k-1": "unknown"}) is not None


@pending("[V2.15] Q9: settled records never refuse")
@pytest.mark.parametrize(
    "outcome", ["delivered_verified", "done_already", "not_delivered", "no_effect"]
)
def test_settled_records_let_the_task_start(outcome: str) -> None:
    assert start_refusal([record("k-1")], {"k-1": outcome}) is None


@pending("[V2.15] Q9: no records, no refusal")
def test_no_records_let_the_task_start() -> None:
    assert start_refusal([], {}) is None
