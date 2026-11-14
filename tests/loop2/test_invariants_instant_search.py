"""Step 3d: the step-3 loop on the instant-search cases (a) to (d) (step 3 packet §5, [V2.1],
[V2.12]), against the 3c stubs. The scripted world plays each variant of
`steerai/probes/instant_search.py`; the probe's positive controls show the real page reaches the
same end states."""

from __future__ import annotations

import dataclasses

from steerai.loop2.decision import Claim, Decision
from steerai.loop2.loop import Reason, run_task
from steerai.world.effects import Observed
from tests.loop2.world_fakes import (
    FIELD,
    GO,
    PAGE,
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
FOCUSED = observed(focus=FIELD.runtime_id)


def _results(*, url_query: bool) -> Observed:
    screen = observed([*PAGE, RESULT], focus=FIELD.runtime_id)
    return dataclasses.replace(screen, url=URL + "?q=red%20shoes" if url_query else URL)


def _verbs(world: World) -> list[str]:
    return [d.action.verb for d in world.dispatched]


@pending("[V2.12] 3d case (a): results and ?q= while typing are done, no Enter sent")
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


@pending("[V2.13].3 3d case (b), script 1: a content_present claim is blocked not_in_step")
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


@pending("[V2.13].3 3d case (b), script 2: a controller stop with blocked is controller_stop")
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


@pending("[V2.12] 3d case (c-button): Enter is a no-op, done via the search button")
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


@pending("[V2.13].2 3d case (c-nobutton): paused no_progress, the Enter sent exactly once")
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


@pending("[V2.12] 3d case (d): slow results after the Enter are judged since the baseline")
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
