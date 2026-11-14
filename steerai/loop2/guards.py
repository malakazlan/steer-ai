"""The step 3 guards: budgets and no-progress strikes ([V2.4]; step 3 packet §3).

Interface stub for step 3c; the tests in `tests/loop2/test_invariants_guards.py` come first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from steerai.loop2.task import Budgets
from steerai.world.effects import EffectReport

BudgetToken = Literal["max_actions", "max_model_calls", "max_cost", "max_wall"]
StrikeReason = Literal[
    "effect_none", "effect_unknown", "same_action_same_effect", "state_seen", "claim_rejected"
]
ActionKey = tuple[str, tuple[int, ...] | None, str | None]
"""(verb, the target's RuntimeId, key): what "the same action" means."""


@dataclass(frozen=True)
class Usage:
    actions: int
    model_calls: int
    cost_usd: float
    wall_s: float


def budget_hit(budgets: Budgets, used: Usage) -> BudgetToken | None:
    """The first budget reached, as a token (the task pauses and asks), or None."""
    raise NotImplementedError("3c: budgets")


@dataclass(frozen=True)
class TurnFacts:
    """One controller turn, as closed facts."""

    ticked: bool
    """A subgoal was proven this turn."""
    action: ActionKey | None
    effect: EffectReport | None
    state_digest: str
    """The element-set digest after the turn."""
    claim_rejected: bool


class Strikes:
    """Counts turns that tick no subgoal and produce no new state; any tick or new state resets
    the count. At `limit` the task pauses."""

    def __init__(self, limit: int) -> None:
        self.limit = limit

    def record(self, turn: TurnFacts) -> StrikeReason | None:
        """The strike this turn earns, or None (and the count resets)."""
        raise NotImplementedError("3c: strikes")

    @property
    def paused(self) -> bool:
        raise NotImplementedError("3c: strikes")

    def may_send(self, action: ActionKey) -> bool:
        """False for the same (verb, target, key) as the last action sent: never twice in a
        row."""
        raise NotImplementedError("3c: strikes")
