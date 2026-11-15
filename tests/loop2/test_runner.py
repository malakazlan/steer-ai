"""Decisions taken in `run_task` (PR A) that no earlier invariant pinned, written after the code:
the target's value is read from the target itself just before dispatch ([V2.28] item 3, [V2.29]
Q2) and never from a secure field; a `world` refusal (the focus, a delivery) is a token and a
strike but no model fault, so it leaves the status to the stop."""

from __future__ import annotations

import pytest

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
    assert world.reads == [FIELD.runtime_id, GO.runtime_id]  # [V2.64] Two: the scope, then GO
    assert world.events.index("read") < world.events.index("act")


def test_a_secure_field_is_never_read() -> None:
    world = World([decision(act("click", target=PASSWORD.id), first=True)])
    run_task(task(), world.ports())
    assert PASSWORD.runtime_id not in world.reads


def test_a_press_refused_for_the_focus_is_recorded_after_the_refusal() -> None:
    """[V2.37] P5: a `world` refusal replaces the latest refusal; a following stop keeps its own
    value, with the reason `after_refusal:<token>`."""
    world = World(
        [decision(act("click", target=FIELD.id), act("press", key="Enter"), first=True)],
        effects=[report("focus")],
        screen=observed(QUERY_PAGE, focus=GO.runtime_id),
    )
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("cannot", "after_refusal:press_not_at_focus")


def test_nothing_delivered_is_recorded_after_the_refusal() -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True)],
        results=[act_result("click", Delivery.NOT_DELIVERED)],
    )
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("cannot", "after_refusal:not_delivered")


def test_the_outcome_carries_the_tasks_wall_time_on_the_ports_clock() -> None:
    """7b: the final JSON reports use against `max_wall_s` from the runner's own clock."""
    world = World([decision(act("click", target=GO.id), first=True), decision(act("wait"))])
    world.watch_s = 2.5
    outcome = run_task(task(), world.ports())
    assert outcome.wall_s == pytest.approx(world.now)
    assert outcome.wall_s >= 2.5
