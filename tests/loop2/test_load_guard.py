"""[V2.31] the loop on a page that may still be loading: give up only on a settled observation
(the load guard), and a wait chosen while the page grows is not a strike."""

from __future__ import annotations

import dataclasses

from steerai.loop2.decision import Decision
from steerai.loop2.runner import LOAD_GUARD_S, run_task
from steerai.orchestrator.loop import REUSE_FRESHNESS_S
from steerai.world.effects import Observed
from steerai.world.observe2 import YOUNG_S
from steerai.world.reads import ReadCost
from tests.loop2.world_fakes import (
    GO,
    MENU,
    PAGE,
    World,
    act,
    decision,
    element,
    observed,
    report,
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
    world.screen = dataclasses.replace(observed(), growing=True, document_since=0.0)
    outcome = run_task(task(), world.ports())
    assert outcome.strikes == ()
    assert outcome.status == "cannot"


def test_a_wait_on_a_settled_page_is_still_a_strike() -> None:
    world = World([decision(act("wait"), first=True), *(decision(act("wait")) for _ in range(4))])
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("paused", "no_progress")


# ---- [V2.34], [V2.35] (2): the young page, slow reads, read costs ------------------------------


def test_the_load_guard_is_the_young_page_age() -> None:
    """One number for both ([V2.35] (2)): a page is young exactly while the guard holds."""
    assert LOAD_GUARD_S == YOUNG_S


def test_a_wait_on_a_young_page_is_not_a_strike() -> None:
    world = World([decision(act("wait"), first=True), *(decision(act("wait")) for _ in range(4))])
    world.screen = dataclasses.replace(observed(), young=True, document_since=0.0)
    outcome = run_task(task(), world.ports())
    assert outcome.strikes == ()


def test_each_action_carries_the_reads_its_watch_made() -> None:
    """[V2.34]: read counts and milliseconds per action, for the wall-time split."""
    cost = ReadCost(poll_reads=3, poll_ms=90, full_reads=1, full_ms=240)
    world = World([decision(act("click", target=GO.id), first=True)], read_cost=cost)
    outcome = run_task(task(), world.ports())
    assert outcome.read_costs == (cost,)


def test_with_two_second_reads_a_fresh_after_is_still_reused() -> None:
    """[V2.34]: every read takes 2 s on a fake clock; the after is stamped at its completion, so
    it is 0 s old when the next batch action runs and is reused with no extra read."""
    world = World(
        [decision(act("click", target=GO.id), act("click", target=MENU.id), first=True)],
        effects=[report("own_state"), report("own_state")],
        read_s=2.0,
    )
    run_task(task(), world.ports())
    acts = [i for i, e in enumerate(world.events) if e == "act"]
    assert "observe" not in world.events[acts[0] + 1 : acts[1]]


def test_with_two_second_reads_an_after_older_than_the_limit_is_read_again() -> None:
    world = World(
        [decision(act("click", target=GO.id), act("click", target=MENU.id), first=True)],
        effects=[report("own_state"), report("own_state")],
        read_s=2.0,
        after_age_s=REUSE_FRESHNESS_S + 1.0,
    )
    run_task(task(), world.ports())
    acts = [i for i, e in enumerate(world.events) if e == "act"]
    assert "observe" in world.events[acts[0] + 1 : acts[1]]


def _page(nodes: int, *, young: bool, since: float) -> Observed:
    rows = [element(100 + i, "ListItem", f"Row {i}", (7, 100 + i), 1) for i in range(nodes)]
    return dataclasses.replace(observed([*PAGE, *rows]), young=young, document_since=since)


def test_gh1_a_partial_young_page_then_the_rest_then_a_cannot_re_asked() -> None:
    """[V2.35] (2), the gh1 case with a scripted controller: handed over partial (101 nodes,
    young); the controller waits; a later read shows 303; a cannot is re-asked once by the load
    guard, and the waits on the young page are no strikes."""
    world = World([decision(act("wait"), first=True), CANNOT, CANNOT])
    world.now = 100.0
    world.screen = _page(101, young=True, since=99.0)
    world.later = _page(303, young=True, since=99.0)
    world.later_after_acts = 1
    outcome = run_task(task(), world.ports())
    assert len(world.views[0].observed.snapshot.elements) == len(PAGE) + 101
    assert len(world.views[1].observed.snapshot.elements) == len(PAGE) + 303
    assert world.events.count("settle") == 1
    assert len(world.views) == 3, "the wait turn, the cannot, the re-ask"
    assert outcome.strikes == ()
    assert outcome.status == "cannot"


# ---- the tester's gaps on #320/#321 (mutants M2, M2b, X1, X2, X3) ------------------------------


def test_t1_a_next_target_renamed_mid_batch_is_not_dispatched_and_not_refused() -> None:
    """§8.1 at loop level: the batch ends on a renamed next target; no refusal, no dispatch."""
    renamed = dataclasses.replace(MENU, name="Something else")
    after = observed([e if e.id != MENU.id else renamed for e in PAGE])
    world = World(
        [decision(act("click", target=GO.id), act("click", target=MENU.id), first=True)],
        effects=[report("own_state")],
        after=after,
    )
    run_task(task(), world.ports())
    assert [d.action.target for d in world.dispatched] == [GO.id]
    assert not any(t.startswith("refused:") for v in world.views for t in v.history)


def test_t2_a_listed_element_with_no_runtime_id_is_never_dispatched() -> None:
    bare = element(11, "Button", "Bare", (), 1)
    world = World([decision(act("click", target=bare.id), first=True)])
    world.screen = observed([*PAGE, bare])
    run_task(task(), world.ports())
    assert world.dispatched == []


def test_t3_the_held_cannot_turn_is_not_a_strike() -> None:
    assert run_task(task(), young(4.0).ports()).strikes == ()


def test_t3b_the_held_cannot_turn_after_a_struck_turn_adds_no_strike() -> None:
    world = young(4.0)
    world.decisions = [decision(act("click", target=GO.id), first=True), CANNOT, CANNOT]
    outcome = run_task(task(), world.ports())
    assert world.events.count("settle") == 1
    assert outcome.strikes == (("effect_none",),)


def test_t4_a_page_exactly_the_guard_old_is_not_held() -> None:
    world = young(LOAD_GUARD_S)
    run_task(task(), world.ports())
    assert world.events.count("settle") == 0


def test_t5_an_after_exactly_the_freshness_limit_old_is_reused() -> None:
    world = World(
        [decision(act("click", target=GO.id), act("click", target=MENU.id), first=True)],
        effects=[report("own_state"), report("own_state")],
        read_s=1.0,
        after_age_s=REUSE_FRESHNESS_S,
        now=100.0,
    )
    run_task(task(), world.ports())
    acts = [i for i, e in enumerate(world.events) if e == "act"]
    assert "observe" not in world.events[acts[0] + 1 : acts[1]]


def test_t6_a_document_time_in_the_future_never_waits_past_the_guard() -> None:
    world = young(-5.0)
    run_task(task(), world.ports())
    assert world.settle_guards
    assert all(g <= LOAD_GUARD_S for g in world.settle_guards), world.settle_guards


# ---- [V2.38] RT-12, RT-24; P3; RT-23; RT-11 ----------------------------------------------------


def test_rt12_the_re_ask_is_once_per_top_document() -> None:
    """A new top Document gets its own re-ask."""
    world = young(4.0)
    new_doc = dataclasses.replace(PAGE[0], runtime_id=(9, 777))
    world.settled = dataclasses.replace(observed([new_doc, *PAGE[1:]]), document_since=100.0)
    world.decisions = [CANNOT, CANNOT, CANNOT, CANNOT]
    run_task(task(), world.ports())
    assert world.events.count("settle") == 2


def test_rt24_the_wait_exemption_ends_with_the_documents_guard() -> None:
    """One total per Document: on a page older than the guard, a wait while growing strikes."""
    world = World([decision(act("wait"), first=True), *(decision(act("wait")) for _ in range(4))])
    world.now = 100.0
    world.screen = dataclasses.replace(
        observed(), growing=True, document_since=100.0 - LOAD_GUARD_S - 1.0
    )
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("paused", "no_progress")


def test_p3_a_wait_beside_a_refused_action_is_still_a_strike() -> None:
    world = World([decision(act("wait"), act("click", target=99), first=True) for _ in range(4)])
    world.screen = dataclasses.replace(observed(), growing=True, document_since=0.0)
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("paused", "no_progress")


def test_rt23_the_settle_guard_is_capped_by_the_wall_budget_left() -> None:
    world = young(4.0)
    world.decisions = [CANNOT, CANNOT]
    budgets = dataclasses.replace(task().budgets, max_wall_s=3.0)
    run_task(task(budgets), world.ports())
    assert world.settle_guards == [3.0]


def test_rt23_a_kill_during_the_settle_stops_the_task() -> None:
    world = young(4.0)
    world.interrupt_after_settle = True
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "takeover")


def test_rt11_every_observation_goes_through_the_one_stamp() -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True)],
        effects=[report("own_state")],
        after=observed(),
    )
    run_task(task(), world.ports())
    assert world.events.count("stamp") >= 2, "the first read and the watch's after"
