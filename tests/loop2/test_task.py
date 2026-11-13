from __future__ import annotations

import dataclasses

import pytest

from steerai.loop2.task import (
    Budgets,
    Envelope,
    GoalSpan,
    Grant,
    Subgoal,
    Task,
    Turn,
)

GOAL = "write hello world to the note"


def test_goal_span_valid() -> None:
    span = GoalSpan.of(GOAL, 6, 17)
    assert span.text == "hello world"
    assert (span.start, span.end) == (6, 17)


def test_goal_span_mismatch_rejected() -> None:
    with pytest.raises(ValueError, match="goal_span_mismatch"):
        GoalSpan.check(GOAL, GoalSpan("hello", 0, 5))


def test_goal_span_out_of_range_rejected() -> None:
    with pytest.raises(ValueError, match="goal_span_mismatch"):
        GoalSpan.of(GOAL, 10, 999)
    with pytest.raises(ValueError, match="goal_span_mismatch"):
        GoalSpan.of(GOAL, -1, 3)


def test_goal_span_check_accepts_match() -> None:
    GoalSpan.check(GOAL, GoalSpan("note", 25, 29))


def test_dataclasses_are_frozen() -> None:
    sub = Subgoal(id="s1", desc="d", params={})
    assert sub.commit == "none"
    assert sub.status == "open"
    assert sub.baseline_seq is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        sub.status = "done"  # type: ignore[misc]  # frozen on purpose, the test asserts it


def test_task_assembles() -> None:
    grant = Grant(commit="send", content=None, object=None)
    env = Envelope(grants=(grant,), mode="bootstrap")
    budgets = Budgets(max_actions=10, max_model_calls=5, max_cost_usd=1.0, max_wall_s=60.0)
    turn = Turn(seq=1, subgoal="s1", verb="click", target=3, outcome="ok")
    task = Task(
        id="t1",
        goal=GOAL,
        subgoals=(Subgoal(id="s1", desc="d", params={}),),
        envelope=env,
        budgets=budgets,
        history=[turn],
    )
    assert task.history[0].verb == "click"
