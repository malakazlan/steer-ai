"""Decisions taken in `run_task` (PR A) that no earlier invariant pinned, written after the code:
the target's value is read from the target itself just before dispatch ([V2.28] item 3, [V2.29]
Q2) and never from a secure field; a `world` refusal (the focus, a delivery) is a token and a
strike but no model fault, so it leaves the status to the stop."""

from __future__ import annotations

from steerai.action.dispositions import Delivery
from steerai.loop2.runner import run_task
from tests.loop2.world_fakes import (
    FIELD,
    GO,
    PASSWORD,
    QUERY_PAGE,
    World,
    act,
    act_result,
    decision,
    observed,
    report,
    task,
)


def test_the_target_value_is_read_from_the_target_just_before_dispatch() -> None:
    world = World([decision(act("click", target=GO.id), first=True)])
    run_task(task(), world.ports())
    assert world.reads == [GO.runtime_id]
    assert world.events.index("read") < world.events.index("act")


def test_a_secure_field_is_never_read() -> None:
    world = World([decision(act("click", target=PASSWORD.id), first=True)])
    run_task(task(), world.ports())
    assert PASSWORD.runtime_id not in world.reads


def test_a_press_refused_for_the_focus_leaves_the_status_to_the_stop() -> None:
    """A `world` refusal (the focus, a delivery) is a token and a strike, never a model fault."""
    world = World(
        [decision(act("click", target=FIELD.id), act("press", key="Enter"), first=True)],
        effects=[report("focus")],
        screen=observed(QUERY_PAGE, focus=GO.runtime_id),
    )
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("cannot", "controller_stop")


def test_nothing_delivered_leaves_the_status_to_the_stop() -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True)],
        results=[act_result("click", Delivery.NOT_DELIVERED)],
    )
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("cannot", "controller_stop")
