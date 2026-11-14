"""Step 3d: the step-3 loop on the instant-search cases (a) to (d) (step 3 packet §5, [V2.1],
[V2.12]), against the 3c stubs. The scripted world plays each variant of
`steerai/probes/instant_search.py`; the probe's positive controls show the real page reaches the
same end states."""

from __future__ import annotations

import dataclasses

from steerai.loop2.decision import Claim, Decision
from steerai.loop2.loop import Reason
from steerai.loop2.runner import run_task
from steerai.world.effects import Observed
from tests.loop2.world_fakes import (
    FIELD,
    GO,
    QUERY_PAGE,
    RESULT,
    URL,
    World,
    act,
    decision,
    observed,
    pending,
    report,
    task,
)

TYPE = act("type", target=FIELD.id, param="text")
ENTER = act("press", key="Enter")
FOCUSED = observed(QUERY_PAGE, focus=FIELD.runtime_id)


def _results(*, url_query: bool) -> Observed:
    screen = observed([*QUERY_PAGE, RESULT], focus=FIELD.runtime_id)
    return dataclasses.replace(screen, url=URL + "?q=red%20shoes" if url_query else URL)


def _verbs(world: World) -> list[str]:
    return [d.action.verb for d in world.dispatched]


@pending("3c PR B: the navigated auto proof ([V2.29] Q1)")
def test_a_results_and_url_while_typing_is_done_without_the_enter() -> None:
    world = World(
        [decision(TYPE, ENTER, first=True)],
        effects=[report("own_value", "content_changed", "url_changed_in_place")],
        screen=FOCUSED,
        later=_results(url_query=True),
    )
    outcome = run_task(task(), world.ports())
    assert outcome.status == "done"
    assert "press" not in _verbs(world)


def test_b_results_only_with_a_content_claim_is_blocked_not_in_step() -> None:
    claim = Claim(subgoal="s1", kind="content_present", evidence=[RESULT.id])
    world = World(
        [decision(TYPE, ENTER, first=True), Decision(claims=[claim])],
        effects=[report("own_value", "content_changed")],
        screen=FOCUSED,
        later=_results(url_query=False),
    )
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("blocked", "not_in_step")
    assert "press" not in _verbs(world), "the batch ended at the results change"


def test_b_results_only_with_a_blocked_stop_is_controller_stop() -> None:
    world = World(
        [decision(TYPE, ENTER, first=True), Decision(stop="blocked")],
        effects=[report("own_value", "content_changed")],
        screen=FOCUSED,
        later=_results(url_query=False),
    )
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("blocked", "controller_stop")
    assert outcome.cause == Reason("controller_stop")


@pending("3c PR B: the navigated auto proof ([V2.29] Q1)")
def test_c_button_is_done_via_the_button_after_one_enter() -> None:
    world = World(
        [decision(TYPE, ENTER, first=True), decision(act("click", target=GO.id))],
        effects=[report("own_value"), report("none"), report("url_changed_in_place")],
        read_backs=[True],
        screen=FOCUSED,
        later=_results(url_query=True),
        later_after_acts=3,
    )
    outcome = run_task(task(), world.ports())
    assert outcome.status == "done"
    assert _verbs(world) == ["type", "press", "click"]


def test_c_nobutton_ends_paused_no_progress_never_the_same_enter_twice() -> None:
    world = World(
        [decision(TYPE, ENTER, first=True), *(decision(ENTER) for _ in range(5))],
        effects=[report("own_value"), report("none")],
        read_backs=[True],
        screen=FOCUSED,
    )
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("paused", "no_progress")
    assert outcome.strikes, "the strike reasons are the evidence"
    assert all(strike for strike in outcome.strikes), "every strike records its reasons"
    assert _verbs(world).count("press") == 1
    assert outcome.reason != "unexpected_effect"


@pending("3c PR B: the navigated auto proof ([V2.29] Q1)")
def test_d_slow_results_are_judged_since_the_baseline() -> None:
    world = World(
        [decision(TYPE, ENTER, first=True)],
        effects=[report("none"), report("none")],
        read_backs=[True],
        screen=FOCUSED,
        later=_results(url_query=True),
        later_after_acts=2,
    )
    outcome = run_task(task(), world.ports())
    assert outcome.status == "done"
    assert _verbs(world) == ["type", "press"]
    assert outcome.reason != "unexpected_effect"
