"""Task state for the V2 loop: the goal, its subgoals, the grants and the budgets.

Everything here is a frozen value. Free text from the model or the screen never enters a Turn;
history holds closed tokens written by code.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

Commit = Literal["none", "send", "submit", "delete", "pay"]
Status = Literal["open", "done", "blocked"]
Mode = Literal["bootstrap", "envelope"]
Closes = Literal["value", "state", "page"]
"""[V2.39]: the kind of fact that closes a subgoal."""
Wanted = Literal["on", "off", "selected", "expanded", "collapsed"]
"""[V2.78]: the state a state subgoal declares at intake, the closed set code can read."""


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
    closes: Closes
    """[V2.39] the kind of fact that closes it, frozen at intake."""
    commit: Commit = "none"
    status: Status = "open"
    baseline_seq: int | None = None
    wanted: Wanted | None = None
    """[V2.78] the state a `closes=state` subgoal declares at intake; None for the other kinds."""


def known_params(subgoals: Iterable[Subgoal]) -> dict[str, frozenset[str]]:
    """The `known_subgoals` map for `validate`: every declared subgoal id, with or without params,
    mapped to its param names. A subgoal missing from the map is `subgoal_unknown`."""
    return {sub.id: frozenset(sub.params) for sub in subgoals}


@dataclass(frozen=True)
class Grant:
    commit: Commit
    content: GoalSpan | None
    object: GoalSpan | None


@dataclass(frozen=True)
class Envelope:
    grants: tuple[Grant, ...]
    mode: Mode


@dataclass(frozen=True, kw_only=True)
class TokenPrice:
    """[V2.37] P6, [V2.45]: the configured price of the model's tokens, in dollars per token.
    Rates are configuration, never constants, and carry the page they were read from and the day
    they were read."""

    usd_per_input_token: float
    usd_per_output_token: float
    usd_per_cache_write_5m_token: float | None = None
    """[V2.45]: the rate of a cache write that lives 5 minutes; None: not configured."""
    usd_per_cache_write_1h_token: float | None = None
    """[V2.45]: the rate of a cache write that lives 1 hour; None: not configured."""
    usd_per_cache_read_token: float | None = None
    """[V2.40] Q3: the cache-read rate; None: not configured."""
    source: str
    """[V2.45]: the https page the rates were read from."""
    read_on: date
    """[V2.45]: the day they were read."""

    def __post_init__(self) -> None:
        """RT-32: a price is a finite positive number; 0 would read as free, never enforced.
        A cache rate may be left out (not configured), never 0."""
        for rate in self.rates():
            if rate is not None and not _positive(rate, integer=False):
                raise ValueError("a token price must be finite and positive")
        source = self.source
        if (
            not isinstance(source, str)
            or not source.startswith("https://")
            or not source.isprintable()
            or " " in source
        ):
            raise ValueError("a price source must be an https page")
        if type(self.read_on) is not date:  # a datetime is not the day it was read
            raise ValueError("a price read_on must be a date")

    def rates(self) -> tuple[float, float, float | None, float | None, float | None]:
        """The rates in `TokenUsage` order: input, output, 5-minute write, 1-hour write, read."""
        return (
            self.usd_per_input_token,
            self.usd_per_output_token,
            self.usd_per_cache_write_5m_token,
            self.usd_per_cache_write_1h_token,
            self.usd_per_cache_read_token,
        )


@dataclass(frozen=True)
class Budgets:
    """Invariant 9 ([V2.37] P6): model calls, tokens, wall time, and cost when a price is
    configured. With no price the cost budget is not configured, and never looks enforced."""

    max_actions: int
    """[V2.42] RT-41: attempts: every dispatch counts, delivered or not; v1's one local retry
    inside an actuation is one attempt."""
    max_model_calls: int
    max_tokens: int
    max_cost_usd: float
    max_wall_s: float
    price: TokenPrice | None = None

    def __post_init__(self) -> None:
        """RT-32: every cap is finite, the counts are positive whole numbers and the wall time is
        positive: an inf or NaN cap would never trip, so the budget would only look enforced.
        The cost cap may be 0 (spend nothing once a price is set)."""
        for name in ("max_actions", "max_model_calls", "max_tokens"):
            if not _positive(getattr(self, name), integer=True):
                raise ValueError(f"{name} must be a positive whole number")
        if not _positive(self.max_wall_s, integer=False):
            raise ValueError("max_wall_s must be finite and positive")
        cost = self.max_cost_usd
        if isinstance(cost, bool) or not isinstance(cost, int | float):
            raise ValueError("max_cost_usd must be a finite number, zero or more")
        if not (math.isfinite(cost) and cost >= 0):  # 0 spends nothing once a price is set
            raise ValueError("max_cost_usd must be a finite number, zero or more")


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


def _positive(value: object, *, integer: bool) -> bool:
    """A finite number above zero; a bool is never a number here, and an integer cap is an int."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    if integer and not isinstance(value, int):
        return False
    return math.isfinite(value) and value > 0
