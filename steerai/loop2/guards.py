"""The step 3 guards: budgets and no-progress strikes ([V2.4], [V2.11] decisions 3 and 4; step 3
packet §3).

Interface stub for step 3c; the tests in `tests/loop2/test_invariants_guards.py` come first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from steerai.loop2._stub import stub
from steerai.loop2.task import Budgets
from steerai.world.effects import EffectReport

BudgetToken = Literal["max_actions", "max_model_calls", "max_cost", "max_wall"]
StrikeReason = Literal[
    "effect_none",
    "effect_unknown",
    "same_action_same_effect",
    "state_seen",
    "claim_rejected",
    "refused",
]


@dataclass(frozen=True)
class ActionKey:
    """What "the same action" means ([V2.11] decision 3): verb + params + RuntimeId; when the
    earlier RuntimeId is gone from the current snapshot (a re-render), the ledger identity."""

    verb: str
    params: tuple[str, ...]
    """The action's own closed fields: key, param name, option."""
    runtime_id: tuple[int, ...]
    identity: tuple[str, str, str]
    """The ledger identity: role, normalised name, automation id."""


@dataclass(frozen=True)
class Usage:
    actions: int
    model_calls: int
    cost_usd: float
    wall_s: float


@stub
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
    present: frozenset[tuple[int, ...]]
    """The RuntimeIds in the current snapshot (for the ledger-identity fallback)."""
    refused: bool = False
    """Nothing was dispatched: the loop refused the action with a token ([V2.11] decision 4)."""


class Strikes:
    """Counts turns that tick no subgoal and produce no new state; any tick or new state resets
    the count. One strike per turn, however many reasons hold; every reason is recorded. At
    `limit` the task pauses."""

    def __init__(self, limit: int) -> None:
        self.limit = limit

    @stub
    def record(self, turn: TurnFacts) -> tuple[StrikeReason, ...]:
        """Every reason this turn is a strike (empty: no strike, and the count resets)."""
        raise NotImplementedError("3c: strikes")

    @property
    @stub
    def count(self) -> int:
        raise NotImplementedError("3c: strikes")

    @property
    @stub
    def paused(self) -> bool:
        raise NotImplementedError("3c: strikes")

    @stub
    def may_send(self, action: ActionKey, *, present: frozenset[tuple[int, ...]]) -> bool:
        """[V2.18] False for the same action as the last one sent, unless that send made
        progress: for `click`, `type`, `choose` and the state verbs a strong effect; for `press`
        and `scroll` any effect but `none`, `unknown` or `frame_changed` alone."""
        raise NotImplementedError("3c: strikes")
