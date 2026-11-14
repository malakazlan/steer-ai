"""[V2.17] item 5: the person's kill switch ends the task `stopped` / `killed` (a kill is not a
policy block). It is honoured between actions, during waits and during asks; nothing is
dispatched after it. Red on the 3c loop stub. The ask case is in `test_invariants_consent.py`;
the record in flight is in `test_invariants_record.py`."""

from __future__ import annotations

from steerai.loop2.runner import run_task
from tests.loop2.world_fakes import GO, MENU, World, act, decision, report, task

CLICK_GO = act("click", target=GO.id)
CLICK_MENU = act("click", target=MENU.id)
WAIT = act("wait")


def test_a_kill_before_the_first_action_dispatches_nothing() -> None:
    world = World([decision(CLICK_GO, first=True)], interrupt_after=0, interruption="killed")
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "killed")
    assert world.dispatched == []


def test_a_kill_between_actions_dispatches_nothing_after_it() -> None:
    world = World(
        [decision(CLICK_GO, CLICK_MENU, first=True)],
        effects=[report("own_state")],
        interrupt_after=1,
        interruption="killed",
    )
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "killed")
    assert [d.action.target for d in world.dispatched] == [GO.id]


def test_a_kill_during_a_wait_dispatches_nothing_after_it() -> None:
    world = World(
        [decision(WAIT, first=True), decision(CLICK_GO)],
        interrupt_after=1,
        interruption="killed",
    )
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "killed")
    assert [d.action.verb for d in world.dispatched] == ["wait"]


def test_a_kill_never_ends_blocked() -> None:
    world = World([decision(CLICK_GO, first=True)], interrupt_after=1, interruption="killed")
    outcome = run_task(task(), world.ports())
    assert outcome.status != "blocked"
