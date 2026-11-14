"""[V2.31] the loop on a page that may still be loading: give up only on a settled observation
(the load guard), and a wait chosen while the page grows is not a strike."""

from __future__ import annotations

import dataclasses

from steerai.loop2.decision import Decision
from steerai.loop2.runner import LOAD_GUARD_S, run_task
from tests.loop2.world_fakes import (
    World,
    act,
    decision,
    observed,
    task,
)

CANNOT = Decision(stop="cannot")


def young(age_s: float) -> World:
    """The top Document appeared `age_s` ago (the clock starts at 100 s)."""
    world = World([CANNOT, CANNOT])
    world.now = 100.0
    world.screen = dataclasses.replace(observed(), document_since=100.0 - age_s)
    return world


def test_a_cannot_on_a_four_second_old_page_is_re_asked_once() -> None:
    world = young(4.0)
    outcome = run_task(task(), world.ports())
    assert world.events.count("settle") == 1
    assert len(world.views) == 2, "re-observed and re-asked once"
    assert outcome.status == "cannot"


def test_the_wait_is_bounded_by_what_is_left_of_the_guard() -> None:
    world = young(4.0)
    run_task(task(), world.ports())
    assert world.settle_guards == [LOAD_GUARD_S - 4.0]


def test_a_cannot_on_a_settled_old_page_ends_at_once() -> None:
    world = young(LOAD_GUARD_S + 1.0)
    outcome = run_task(task(), world.ports())
    assert (world.events.count("settle"), len(world.views)) == (0, 1)
    assert outcome.status == "cannot"


def test_a_page_with_no_document_time_is_not_held() -> None:
    world = World([CANNOT, CANNOT])
    run_task(task(), world.ports())
    assert (world.events.count("settle"), len(world.views)) == (0, 1)


def test_the_re_ask_happens_once_per_task() -> None:
    world = young(4.0)
    world.decisions = [CANNOT, CANNOT, CANNOT]
    run_task(task(), world.ports())
    assert (world.events.count("settle"), len(world.views)) == (1, 2)


def test_a_blocked_stop_is_not_held() -> None:
    world = young(4.0)
    world.decisions = [Decision(stop="blocked")]
    run_task(task(), world.ports())
    assert world.events.count("settle") == 0


def test_the_strike_limit_on_a_young_page_is_re_asked_once() -> None:
    world = young(4.0)
    world.decisions = [Decision(claims=[]) for _ in range(6)]
    outcome = run_task(task(), world.ports())
    assert world.events.count("settle") == 1
    assert (outcome.status, outcome.reason) == ("paused", "no_progress")
    assert len(world.views) == 4, "three strikes, the hold, one more turn"


def test_a_wait_while_the_page_grows_is_not_a_strike() -> None:
    world = World([decision(act("wait"), first=True), *(decision(act("wait")) for _ in range(4))])
    world.screen = dataclasses.replace(observed(), growing=True)
    outcome = run_task(task(), world.ports())
    assert outcome.strikes == ()
    assert outcome.status == "cannot"


def test_a_wait_on_a_settled_page_is_still_a_strike() -> None:
    world = World([decision(act("wait"), first=True), *(decision(act("wait")) for _ in range(4))])
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("paused", "no_progress")
