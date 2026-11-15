"""[V2.68] (D's answer), coordinator-approved fixes (a) and (b).

(a) A Decision the controller still rejects after its repair is a refused turn, not the end of
the task: its rejection tokens go to the history as refusal lines and to the counts, the turn
takes one strike, nothing is frozen (an intake turn stays an intake turn), and the task ends only
through the strike limit or a budget. A controller error with no call made (the prompt over its
bound) keeps today's path.

(b) Invariant 9 ([V2.37] P6): `max_model_calls` counts every model call. A repaired turn is two
calls, and so is a turn whose repair was rejected too."""

from __future__ import annotations

import pytest

from steerai.loop2.controller import PROMPT_VERSION, ControllerError, DecideRecord
from steerai.loop2.decision import Decision, SubgoalDoc
from steerai.loop2.loop import TokenUsage
from steerai.loop2.runner import run_task
from steerai.loop2.task import Budgets
from tests.loop2.world_fakes import FIELD, GO, World, act, report, task

SUBS = [SubgoalDoc(id="s1", desc="x", params={"text": "red shoes"}, closes="page")]


def _rejected(*tokens: str) -> ControllerError:
    error = ControllerError("rejected", tokens)
    error.record = DecideRecord(PROMPT_VERSION, 2, True, True)  # a call and its repair
    error.usage = TokenUsage(200, 20)  # both calls reported their usage
    return error


def _budget(calls: int) -> Budgets:
    return Budgets(max_actions=20, max_model_calls=calls, max_tokens=100_000,
                   max_cost_usd=1.0, max_wall_s=60.0)  # fmt: skip


def test_a_rejected_first_decision_is_a_refused_intake_turn() -> None:
    world = World(
        [Decision(subgoals=SUBS, actions=[act("click", target=GO.id)]), Decision(stop="blocked")],
        effects=[report("content_changed")],
    )
    world.decide_errors = [_rejected("subgoal_id_bad"), None, None]
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("blocked", "controller_stop")
    assert "refused:subgoal_id_bad" in world.views[1].history
    assert world.views[1].first_turn  # nothing was frozen: still the intake turn
    assert dict(outcome.refusals)["subgoal_id_bad"] == 1
    assert ("refused",) in outcome.strikes
    assert len(world.dispatched) == 1  # the next turn's own click went out


def test_rejected_turns_end_only_through_the_strike_limit() -> None:
    world = World([])
    world.decide_errors = [_rejected("param_not_span")] * 10
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("paused", "no_progress")
    assert len(world.views) == 3


def test_a_controller_error_with_no_call_keeps_todays_path() -> None:
    world = World([])
    world.decide_errors = [ControllerError("prompt_too_large")]
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "port_prompt_too_large")
    assert outcome.model_calls == 0


def test_a_repaired_turn_counts_two_calls_against_the_budget() -> None:
    world = World(
        [Decision(subgoals=SUBS, actions=[act("click", target=GO.id)]), Decision(stop="blocked")],
        effects=[report("content_changed")],
    )
    world.decide_calls = [2, 1]
    outcome = run_task(task(_budget(2)), world.ports())
    assert outcome.model_calls == 2
    assert (outcome.status, outcome.reason) == ("paused", "max_model_calls")


@pytest.mark.parametrize("budget", [2, 3])
def test_a_rejected_turn_counts_its_two_calls(budget: int) -> None:
    world = World([Decision(stop="blocked")])
    world.decide_errors = [_rejected("subgoal_id_bad"), None]
    outcome = run_task(task(_budget(budget)), world.ports())
    if budget == 2:
        assert (outcome.model_calls, outcome.reason) == (2, "max_model_calls")
    else:
        # the stop right after a refusal keeps it as its reason ([V2.37] P5)
        assert (outcome.model_calls, outcome.reason) == (3, "after_refusal:subgoal_id_bad")


def test_repairs_and_retries_never_stack_past_the_strike_limit() -> None:
    """[V2.69]: decide's repair inside a turn plus the runner's retry across turns: ten
    rejections end at the strike limit, three turns of at most two calls each."""
    world = World([])
    world.decide_errors = [_rejected("subgoal_id_bad")] * 10
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("paused", "no_progress")
    assert len(world.views) == 3
    assert outcome.model_calls == 6


@pytest.mark.parametrize(("budget", "used_first", "left"), [(3, 2, 1), (4, 2, 2), (2, 1, 1)])
def test_each_turn_is_told_the_calls_it_has_left(budget: int, used_first: int, left: int) -> None:
    """Invariant 9 hard cap: decide is given the calls left so its repair never overruns."""
    world = World(
        [Decision(subgoals=SUBS, actions=[act("click", target=GO.id)]), Decision(stop="blocked")],
        effects=[report("content_changed")],
    )
    world.decide_calls = [used_first, 1]
    run_task(task(_budget(budget)), world.ports())
    assert world.views[0].calls_left == budget
    assert world.views[1].calls_left == left


def test_a_close_at_turn_start_survives_a_rejected_decision_in_that_turn() -> None:
    """#437 delta (the merge with #434): s1 closes on the stale re-read at the top of a turn whose
    decide is then rejected. The close is that turn's progress: s1 stays proven and the
    rejection adds no strike on that turn ([V2.67] A with [V2.68])."""
    from tests.loop2.test_auto_proof_every_observation import RESULTS, START  # noqa: PLC0415

    subs = [
        SubgoalDoc(id="s1", desc="results", params={"text": "red shoes"}, closes="page"),
        SubgoalDoc(id="s2", desc="shop", params={"where": "shop"}, closes="page"),
    ]
    world = World(
        [
            Decision(subgoals=subs, actions=[act("type", target=FIELD.id, param="text")]),
            Decision(stop="blocked"),  # decided on the old read: discarded as stale
            Decision(stop="blocked"),
        ],
        effects=[report("own_value")],
        screen=START,
        observations=[START, START, RESULTS],
    )
    world.decide_errors = [None, None, _rejected("subgoal_id_bad"), None]
    outcome = run_task(task(), world.ports())
    assert outcome.strikes == ()
    history = world.views[-1].history
    assert "proven:s1:results_with_url" in history
    assert "refused:subgoal_id_bad" in history
