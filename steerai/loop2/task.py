"""Task state for the V2 loop: the goal, its subgoals, the grants and the budgets.

Everything here is a frozen value. Free text from the model or the screen never enters a Turn;
history holds closed tokens written by code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

Commit = Literal["none", "send", "submit", "delete", "pay"]
Status = Literal["open", "done", "blocked"]
Mode = Literal["bootstrap", "envelope"]


@dataclass(frozen=True)
class GoalSpan:
    """A piece of the user's request, with its position, so it can be re-checked against it."""

    text: str
    start: int
    end: int

    @classmethod
    def of(cls, goal: str, start: int, end: int) -> GoalSpan:
        span = cls(goal[start:end] if 0 <= start <= end <= len(goal) else "", start, end)
        cls.check(goal, span)
        return span

    @classmethod
    def check(cls, goal: str, span: GoalSpan) -> None:
        if not 0 <= span.start <= span.end <= len(goal) or goal[span.start : span.end] != span.text:
            raise ValueError("goal_span_mismatch")


@dataclass(frozen=True)
class Subgoal:
    id: str
    desc: str
    params: Mapping[str, GoalSpan]
    commit: Commit = "none"
    status: Status = "open"
    baseline_seq: int | None = None


@dataclass(frozen=True)
class Grant:
    commit: Commit
    content: GoalSpan | None
    object: GoalSpan | None


@dataclass(frozen=True)
class Envelope:
    grants: tuple[Grant, ...]
    mode: Mode


@dataclass(frozen=True)
class Budgets:
    max_actions: int
    max_model_calls: int
    max_cost_usd: float
    max_wall_s: float


@dataclass(frozen=True)
class Turn:
    """One step of history, closed tokens only: ids, verb and outcome codes, never text."""

    seq: int
    subgoal: str
    verb: str
    target: int | None
    outcome: str


@dataclass(frozen=True)
class Task:
    id: str
    goal: str
    subgoals: tuple[Subgoal, ...]
    envelope: Envelope
    budgets: Budgets
    history: list[Turn]
