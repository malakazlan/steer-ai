"""[V2.51] in the runner (slice 6): focus only on a field (1), a type only where the focus is (3),
Backspace only on Steer's own text (5), `field_not_empty` handed back and counted (5), the second
Toggle counted (4), and the act port's takeover honoured (AP-30)."""

from __future__ import annotations

import pytest

from steerai.action.dispositions import ActionResult, Delivery
from steerai.errors import ErrorCode
from steerai.loop2.controller import PROMPT_VERSION, SYSTEM_RULES
from steerai.loop2.history import REFUSAL_TOKENS
from steerai.loop2.loop import ActResult
from steerai.loop2.runner import REFUSAL_KIND, run_task
from tests.loop2.world_fakes import (
    FIELD,
    GO,
    QUERY_PAGE,
    World,
    act,
    decision,
    element,
    observed,
    report,
    task,
)

TYPE = act("type", target=FIELD.id, param="text")
BOX = element(42, "CheckBox", "Shop", (9, 42), 1)


def _refusals(world: World) -> list[str]:
    return [line for view in world.views for line in view.history if line.startswith("refused:")]


# ---- (1) focus only on a field ---------------------------------------------------------------


def test_focus_on_a_non_field_is_a_model_fault() -> None:
    world = World([decision(act("focus", target=GO.id), first=True)], screen=observed(QUERY_PAGE))
    outcome = run_task(task(), world.ports())
    assert world.dispatched == []
    assert (outcome.status, outcome.reason) == ("cannot", "after_refusal:focus_needs_field")
    assert REFUSAL_KIND.get("focus_needs_field", "model") == "model"


def test_focus_on_a_field_is_dispatched() -> None:
    world = World(
        [decision(act("focus", target=FIELD.id), first=True)], screen=observed(QUERY_PAGE)
    )
    run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["focus"]


# ---- (3) a type goes only where the focus is -------------------------------------------------


def test_a_type_where_the_listing_shows_no_focus_on_the_field_is_a_strike() -> None:
    world = World([decision(TYPE, first=True)], screen=observed(QUERY_PAGE, focus=GO.runtime_id))
    outcome = run_task(task(), world.ports())
    assert world.dispatched == []
    assert outcome.reason == "after_refusal:type_not_at_focus"
    assert outcome.type_unfocused == 1
    assert outcome.strikes  # a strike at once


def test_a_type_where_the_listing_shows_the_focus_on_the_field_is_dispatched() -> None:
    world = World([decision(TYPE, first=True)], screen=observed(QUERY_PAGE, focus=FIELD.runtime_id))
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type"]
    assert outcome.type_unfocused == 0


def test_focus_then_type_in_one_batch() -> None:
    """The controller focuses first ([V2.51] (3)); the batch's own verified focus is what the
    type is checked against, and the after-read shows it there."""
    focused = observed(QUERY_PAGE, focus=FIELD.runtime_id)
    world = World(
        [decision(act("focus", target=FIELD.id), TYPE, first=True)],
        screen=observed(QUERY_PAGE),
        effects=[report("focus")],
        timeline=[(1, focused)],
    )
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["focus", "type"]
    assert outcome.type_unfocused == 0


def test_a_focus_that_did_not_read_back_does_not_clear_the_type() -> None:
    world = World(
        [decision(act("focus", target=FIELD.id), TYPE, first=True)],
        screen=observed(QUERY_PAGE),
        effects=[report("focus")],
        read_backs=[False],
    )
    run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["focus"]  # a mismatch ends the batch


def test_focus_shown_in_the_listing_and_gone_now_is_the_world_moving() -> None:
    """[V2.43] (2): the listing showed the focus on the field and the re-read after the
    decision does not: staleness (re-ask, no strike for the first), counted."""
    listed = observed(QUERY_PAGE, focus=FIELD.runtime_id)
    moved = observed(QUERY_PAGE, focus=GO.runtime_id)
    world = World([decision(TYPE, first=True)], observations=[listed, moved])
    outcome = run_task(task(), world.ports())
    assert world.dispatched == []
    assert outcome.focus_losses >= 1
    assert outcome.type_unfocused >= 1
    assert "refused:type_not_at_focus" not in " ".join(_refusals(world))


# ---- (5) Backspace only on Steer's own text; field_not_empty counted --------------------------


def test_backspace_on_a_field_the_person_filled_is_refused() -> None:
    """No pending-text record for the field: Steer never deletes text it did not type."""
    world = World(
        [decision(act("click", target=FIELD.id), act("press", key="Backspace"), first=True)],
        screen=observed(QUERY_PAGE, focus=FIELD.runtime_id),
        effects=[report("focus")],
    )
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["click"]
    assert outcome.reason == "after_refusal:not_own_text"
    assert outcome.not_own_text == 1


def test_backspace_on_steers_own_typo_is_allowed() -> None:
    world = World(
        [decision(TYPE, act("press", key="Backspace"), first=True)],
        screen=observed(QUERY_PAGE, focus=FIELD.runtime_id),
        effects=[report("own_value")],
    )
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type", "press"]
    assert world.dispatched[1].own_text is True
    assert outcome.not_own_text == 0


def test_a_field_not_empty_goes_back_to_the_controller_and_is_counted() -> None:
    refused = ActionResult(
        "type",
        Delivery.NOT_DELIVERED,
        code=ErrorCode.GATE_PENDING_USER,
        evidence={"reason": "gate_pending_user", "detail": "field_not_empty"},
    )
    world = World(
        [decision(TYPE, first=True)],
        screen=observed(QUERY_PAGE, focus=FIELD.runtime_id),
        results=[ActResult(refused, None)],
    )
    outcome = run_task(task(), world.ports())
    assert outcome.reason == "after_refusal:field_not_empty"
    assert outcome.fields_not_empty == 1


# ---- (4) the second Toggle counted; AP-30 the port's takeover --------------------------------


@pytest.mark.parametrize(
    ("evidence", "delivery", "counts"),
    [
        ({"toggles": 2}, Delivery.DELIVERED_VERIFIED, (1, 0, 0)),
        (
            {"toggles": 1, "detail": "second_toggle_refused"},
            Delivery.DELIVERED_UNVERIFIED,
            (0, 1, 0),
        ),
        ({"toggles": 1, "detail": "settled_on_reread"}, Delivery.DELIVERED_VERIFIED, (0, 0, 1)),
        ({"toggles": 1}, Delivery.DELIVERED_VERIFIED, (0, 0, 0)),
    ],
)
def test_second_toggles_are_counted_fired_refused_and_settled_apart(
    evidence: dict[str, object], delivery: Delivery, counts: tuple[int, int, int]
) -> None:
    """[V2.52] (a), the coordinator: fires, refusals and "settled on re-read" each counted."""
    code = None if delivery is Delivery.DELIVERED_VERIFIED else ErrorCode.VERIFY_FAILED
    checked = ActionResult("check", delivery, code=code, evidence=evidence)
    world = World(
        [decision(act("check", target=BOX.id), first=True)],
        screen=observed([*QUERY_PAGE, BOX]),
        results=[ActResult(checked, delivery is Delivery.DELIVERED_VERIFIED)],
    )
    outcome = run_task(task(), world.ports())
    assert (
        outcome.second_toggles,
        outcome.second_toggles_refused,
        outcome.toggles_settled_on_reread,
    ) == counts


def test_the_act_ports_takeover_stops_the_task() -> None:
    refused = ActionResult(
        "type",
        Delivery.NOT_DELIVERED,
        code=ErrorCode.GATE_PENDING_USER,
        evidence={"detail": "takeover"},
    )
    world = World(
        [decision(TYPE, first=True)],
        screen=observed(QUERY_PAGE, focus=FIELD.runtime_id),
        results=[ActResult(refused, None)],
    )
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "takeover")


# ---- the closed sets and the prompt ----------------------------------------------------------


def test_the_new_refusals_are_closed_tokens_of_their_kinds() -> None:
    new = {"focus_needs_field", "type_not_at_focus", "not_own_text", "field_not_empty"}
    assert new <= REFUSAL_TOKENS
    assert REFUSAL_KIND["type_not_at_focus"] == "world"  # as `press_not_at_focus`
    assert REFUSAL_KIND["field_not_empty"] == "world"
    assert "focus_needs_field" not in REFUSAL_KIND  # a model fault
    assert "not_own_text" not in REFUSAL_KIND


def test_the_prompt_says_to_focus_a_field_before_typing() -> None:
    assert PROMPT_VERSION == "controller-11"
    assert "Focus a text field before you type into it" in SYSTEM_RULES


def test_a_batch_focus_on_another_field_overrides_the_listing() -> None:
    """Principal non-blocking: the listing showed the focus on the field, but this batch's
    verified focus put it on another one: the type is the model's fault, not the world's."""
    other = element(50, "Edit", "Find", (9, 50), 1)
    world = World(
        [decision(act("focus", target=other.id), TYPE, first=True)],
        screen=observed([*QUERY_PAGE, other], focus=FIELD.runtime_id),
        effects=[report("focus")],
    )
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["focus"]
    assert f"refused:type_not_at_focus:{FIELD.id}" in world.views[-1].history
    assert outcome.focus_losses == 0


def test_a_takeover_during_an_acts_bursts_stops_the_task() -> None:
    """[V2.57] LW-05: the guard refused a burst for the person's input; whatever went out, the
    task stops as a takeover."""
    partial = ActionResult(
        "type",
        Delivery.DELIVERY_UNCERTAIN,
        code=ErrorCode.VERIFY_FAILED,
        evidence={"reason": "gate_pending_user", "phase": "call", "detail": "takeover"},
    )
    world = World(
        [decision(TYPE, first=True)],
        screen=observed(QUERY_PAGE, focus=FIELD.runtime_id),
        results=[ActResult(partial, None)],
    )
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "takeover")
    assert [d.action.verb for d in outcome.dispatched] == ["type"]


@pytest.mark.parametrize(
    "delivery", [Delivery.NOT_DELIVERED, Delivery.DELIVERY_UNCERTAIN], ids=["before", "during"]
)
def test_another_steers_input_stops_the_task_as_its_own_reason(delivery: Delivery) -> None:
    """[V2.58] (2): stopped the same way as a takeover, reported as another Steer."""
    refused = ActionResult(
        "type", delivery, code=ErrorCode.GATE_PENDING_USER,
        evidence={"detail": "other_steer_input"},
    )  # fmt: skip
    world = World(
        [decision(TYPE, first=True)],
        screen=observed(QUERY_PAGE, focus=FIELD.runtime_id),
        results=[ActResult(refused, None)],
    )
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "other_steer_input")


def test_the_stop_tokens_are_closed() -> None:
    from steerai.session.monitor import TAKEOVER_TOKENS  # noqa: PLC0415 - this test only

    assert frozenset({"takeover", "other_steer_input"}) == TAKEOVER_TOKENS
