"""The v2 action key and write-ahead record ([V2.15] Q1, Q9; V2 §8.4). The key is v1's scheme
and never holds the subgoal; the record does. The v1 key values are pinned in
`test_ledger_keys.py`; the durable file is tested in `test_wal.py`."""

from __future__ import annotations

import pytest

from steerai.loop2.claims import Phase
from steerai.loop2.record import (
    RELEASED,
    V2Record,
    admission,
    close_token,
    key_for,
    on_kill,
    open_record,
    start_refusal,
    void_token,
)
from steerai.orchestrator.contracts import GateDecision, GateOutcome, Risk, Target
from steerai.orchestrator.ledger import action_key
from steerai.world.effects import EffectClass, Token
from tests.loop2.world_fakes import FIELD, PAGE, SEND, act, element, report, snapshot

SECRET = bytes(range(32))
BEFORE = snapshot(PAGE)


def test_the_same_action_for_two_subgoals_has_one_key() -> None:
    one = key_for(SECRET, act("click", target=SEND.id, subgoal="s1"), SEND, text=None)
    two = key_for(SECRET, act("click", target=SEND.id, subgoal="s2"), SEND, text=None)
    assert one == two


def test_a_click_key_is_v1s_action_key() -> None:
    expected = action_key(SECRET, "click", Target(role="Button", name="Send"), {})
    assert key_for(SECRET, act("click", target=SEND.id), SEND, text=None) == expected


def test_a_type_key_carries_its_text_as_v1_does() -> None:
    expected = action_key(SECRET, "type", Target(role="Edit", name="Search"), {"text": "red shoes"})
    typed = act("type", target=FIELD.id, param="text")
    assert key_for(SECRET, typed, FIELD, text="red shoes") == expected


def test_a_press_key_carries_its_key_param() -> None:
    expected = action_key(SECRET, "press", Target(role="Edit", name="Search"), {"key": "Enter"})
    assert key_for(SECRET, act("press", key="Enter"), FIELD, text=None) == expected


def test_other_text_is_another_key() -> None:
    typed = act("type", target=FIELD.id, param="text")
    assert key_for(SECRET, typed, FIELD, text="red shoes") != key_for(
        SECRET, typed, FIELD, text="blue shoes"
    )


NOTE = element(20, "Edit", "Note", (9, 20), 1)


def test_two_enters_on_different_fields_have_different_keys() -> None:
    enter = act("press", key="Enter")
    assert key_for(SECRET, enter, FIELD, text=None) != key_for(SECRET, enter, NOTE, text=None)


def test_a_type_by_param_and_by_the_same_text_have_one_key() -> None:
    by_param = act("type", target=FIELD.id, param="text")
    by_text = act("type", target=FIELD.id, text="red shoes")
    assert key_for(SECRET, by_param, FIELD, text="red shoes") == key_for(
        SECRET, by_text, FIELD, text="red shoes"
    )


def test_the_declared_commit_is_not_in_the_key() -> None:
    plain = act("click", target=SEND.id)
    declared = act("click", target=SEND.id, commit="send")
    assert key_for(SECRET, plain, SEND, text=None) == key_for(SECRET, declared, SEND, text=None)


def test_a_choose_key_carries_its_option() -> None:
    expected = action_key(SECRET, "choose", Target(role="ComboBox", name="Size"), {"option": "9"})
    size = element(21, "ComboBox", "Size", (9, 21), 1)
    assert key_for(SECRET, act("choose", target=21, option="9"), size, text=None) == expected


def test_an_unnamed_target_has_no_key() -> None:
    """v1's key needs a named target (`Target` refuses an empty name); unclear means fail."""
    icon = element(22, "Image", "  ", (9, 22), 1)
    with pytest.raises(ValueError, match="named"):
        key_for(SECRET, act("click", target=22), icon, text=None)


def test_a_type_without_its_resolved_text_has_no_key() -> None:
    with pytest.raises(ValueError, match="text"):
        key_for(SECRET, act("type", target=FIELD.id, param="text"), FIELD, text=None)


def test_a_resolved_text_other_than_the_actions_own_is_refused() -> None:
    typed = act("type", target=FIELD.id, text="red shoes")
    with pytest.raises(ValueError, match="text"):
        key_for(SECRET, typed, FIELD, text="blue shoes")


def decision(key: str = "k-1", outcome: GateOutcome = "confirm") -> GateDecision:
    return GateDecision(decision_id="d-000000000001", action_key=key, outcome=outcome)


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


def test_opening_a_record_consumes_the_decision() -> None:
    granted = decision()
    open_record(
        action_key="k-1", subgoal="s1", verb="click", role="Button", before=BEFORE, decision=granted
    )
    assert granted.consumed


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


def test_an_approve_decision_never_opens_a_record() -> None:
    """§3.4 step 8: bootstrap mode admits only a `confirm` (a yes); grants are not in step 4."""
    granted = decision(outcome="approve")
    with pytest.raises(ValueError, match="decision"):
        open_record(
            action_key="k-1",
            subgoal="s1",
            verb="click",
            role="Button",
            before=BEFORE,
            decision=granted,
        )
    assert not granted.consumed


def test_a_refused_decision_is_not_consumed() -> None:
    other = decision("k-2")
    with pytest.raises(ValueError, match="decision"):
        open_record(
            action_key="k-1",
            subgoal="s1",
            verb="click",
            role="Button",
            before=BEFORE,
            decision=other,
        )
    assert not other.consumed


def record(
    key: str, verb: str = "click", role: str = "Button", *, phase: Phase = "acting"
) -> V2Record:
    return V2Record(
        action_key=key,
        subgoal="s1",
        decision_id="d-000000000001",
        verb=verb,
        role=role,
        phase=phase,
        before=BEFORE,
    )


def test_an_open_record_refuses_the_start_and_names_the_action() -> None:
    refusal = start_refusal([record("k-1", "press", "Edit")], {"k-1": None})
    assert refusal is not None
    assert (refusal.token, refusal.verb, refusal.role) == ("unsettled_action", "press", "Edit")


def test_an_unknown_outcome_refuses_the_start() -> None:
    assert start_refusal([record("k-1")], {"k-1": "unknown"}) is not None


@pytest.mark.parametrize(
    "outcome", ["delivered_verified", "done_already", "not_delivered", "no_effect"]
)
def test_settled_records_let_the_task_start(outcome: str) -> None:
    assert start_refusal([record("k-1")], {"k-1": outcome}) is None


def test_no_records_let_the_task_start() -> None:
    assert start_refusal([], {}) is None


# ---- closing a record and admitting a key ([V2.17] items 4 to 6) -----------------------------


def test_a_proven_none_closes_no_effect_and_releases_the_key() -> None:
    closed = close_token(report("none"))
    assert closed == "no_effect"
    assert closed in RELEASED


WEAK: list[tuple[tuple[EffectClass, ...], tuple[Token, ...]]] = [
    (("focus",), ()),
    (("own_state",), ()),
    ((), ("frame_changed",)),
]


@pytest.mark.parametrize(("classes", "tokens"), WEAK, ids=["focus", "own_state", "frame"])
def test_weak_only_keeps_the_key_consumed(
    classes: tuple[EffectClass, ...], tokens: tuple[Token, ...]
) -> None:
    closed = close_token(report(*classes, tokens=tokens))
    assert closed == "weak_only"
    assert closed not in RELEASED


def test_unknown_keeps_the_key_consumed() -> None:
    assert close_token(report("unknown")) not in RELEASED
    assert close_token(report("content_changed", unread=["url"])) not in RELEASED


@pytest.mark.parametrize("outcome", ["unknown", "weak_only", None])
def test_a_consumed_key_at_r2_asks_again(outcome: str | None) -> None:
    assert admission("R2", outcome, recorded=True) == "confirm"


@pytest.mark.parametrize("outcome", sorted(RELEASED))
def test_a_released_key_is_fresh(outcome: str) -> None:
    assert admission("R2", outcome, recorded=True) == "fresh"


@pytest.mark.parametrize("outcome", ["delivered_verified", "done_already"])
def test_a_verified_r2_key_is_done(outcome: str) -> None:
    assert admission("R3", outcome, recorded=True) == "done"


@pytest.mark.parametrize(
    ("risk", "repeats"), [("R0", 2), ("R1", 3)], ids=["next_twice", "page_down_three_times"]
)
def test_r0_and_r1_repeats_are_always_fresh(risk: Risk, repeats: int) -> None:
    seen = [admission(risk, "delivered_verified", recorded=True) for _ in range(repeats)]
    assert seen == ["fresh"] * repeats


def test_a_record_in_flight_settles_unknown_on_kill() -> None:
    assert on_kill(record("k-1")) == "unknown"


def test_a_written_record_settles_unknown_on_kill() -> None:
    """Unclear means fail: a kill between the write-ahead and the dispatch never proves that
    nothing was sent; only the gate's own void does (`void_token`)."""
    assert on_kill(record("k-1", phase="written")) == "unknown"


# ---- the void: input after the write-ahead, before the dispatch (principal review of #509) ----


def test_a_void_closes_a_written_record_not_delivered_and_releases_the_key() -> None:
    closed = void_token(record("k-1", phase="written"))
    assert closed == "not_delivered"
    assert closed in RELEASED
    assert admission("R2", closed, recorded=True) == "fresh"


def test_a_void_closed_record_lets_the_next_start_go() -> None:
    written = record("k-1", phase="written")
    assert start_refusal([written], {"k-1": void_token(written)}) is None


def test_an_open_written_record_after_a_crash_refuses_the_next_start() -> None:
    refusal = start_refusal([record("k-1", "press", "Edit", phase="written")], {"k-1": None})
    assert refusal is not None
    assert (refusal.token, refusal.verb, refusal.role) == ("unsettled_action", "press", "Edit")


@pytest.mark.parametrize("phase", ["acting", "verified"])
def test_a_record_past_written_is_never_voided(phase: Phase) -> None:
    """`acting` is written before the dispatch is called: from then on nothing proves the
    dispatch did not happen."""
    with pytest.raises(ValueError, match="void"):
        void_token(record("k-1", phase=phase))
