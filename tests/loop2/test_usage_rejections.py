"""The 3d shakedown's #2 and #3 (2026-11-15): a turn the controller rejects after its repair keeps
the usage its calls reported (never unknown when it was reported), and the run's final line
names the rejection with closed tokens."""

from __future__ import annotations

import json
import math

import pytest

from steerai.loop2.controller import ControllerError, DecideRecord
from steerai.loop2.decide_port import DecideCalls, Reply, TaskGenerations
from steerai.loop2.decision import Decision
from steerai.loop2.loop import TokenUsage
from steerai.loop2.prompt_inputs import make_decide
from steerai.loop2.runner import run_task
from tests.loop2.test_listing_wiring import GOAL, _view
from tests.loop2.world_fakes import World, act, decision, task


class _Rejected:
    """Two replies the controller rejects (a target never listed), each with usage."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _system: list[str], _blocks: list[str]) -> Reply:
        self.calls += 1
        doc = {"actions": [{"verb": "click", "target": 999_999, "subgoal": "s1"}],
               "subgoals": [{"id": "s1", "desc": "x", "params": {"text": "red shoes"},
                             "closes": "page"}]}  # fmt: skip
        return Reply(json.dumps(doc), "end_turn", TokenUsage(100, 20))


def test_a_rejected_turn_carries_the_usage_of_both_its_calls() -> None:
    """Reproduced offline: the decide port summed the two calls' usage, then dropped it with
    the ControllerError, so the runner could only mark the run's usage unknown."""
    provider = _Rejected()
    calls = DecideCalls(
        provider, interrupted=lambda: None, generations=TaskGenerations(),
        late_error=lambda _t: None,
    )  # fmt: skip
    with pytest.raises(ControllerError) as rejected:
        make_decide(calls, goal=GOAL, task_fence="taskfence01")(_view())
    assert provider.calls == 2  # the call and its repair
    assert rejected.value.usage == TokenUsage(200, 40)
    assert "id_not_listed" in rejected.value.tokens


def test_the_runner_counts_a_rejected_turns_usage_and_names_the_rejection() -> None:
    failed = ControllerError("rejected", ("id_not_listed",))
    failed.usage = TokenUsage(200, 40)
    failed.record = DecideRecord("controller-13", 2, True, True)  # the call and its repair
    world = World([Decision(stop="blocked")])
    world.decide_errors = [failed]  # [V2.68]: a refused turn, then the task goes on
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("blocked", "after_refusal:id_not_listed")
    assert outcome.usage_incomplete is False
    assert outcome.tokens == 240.0 + 110.0  # the rejected turn's two calls, then the stop's
    assert outcome.rejections == ("id_not_listed",)


def test_an_unreported_usage_on_a_rejected_turn_stays_unknown() -> None:
    failed = ControllerError("rejected", ("schema",))
    failed.record = DecideRecord("controller-13", 2, True, True)  # the call and its repair
    world = World([decision(act("wait"), first=True)])
    world.decide_errors = [failed]
    outcome = run_task(task(), world.ports())
    assert outcome.usage_incomplete is True
    assert math.isnan(outcome.tokens)
