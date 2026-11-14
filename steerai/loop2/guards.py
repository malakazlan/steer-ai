"""The step 3 guards: budgets and no-progress strikes ([V2.4], [V2.11] decisions 3 and 4, [V2.18];
step 3 packet §3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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


def budget_hit(budgets: Budgets, used: Usage) -> BudgetToken | None:
    """The first budget reached, as a token (the task pauses and asks), or None."""
    if used.actions >= budgets.max_actions:
        return "max_actions"
    if used.model_calls >= budgets.max_model_calls:
        return "max_model_calls"
    if used.cost_usd >= budgets.max_cost_usd:
        return "max_cost"
    if used.wall_s >= budgets.max_wall_s:
        return "max_wall"
    return None


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


STRONG = frozenset(
    {"navigated", "url_changed_in_place", "content_changed", "revealed", "dialog_opened"}
)
"""[V2.18] the effects after which a click, a type, a choose or a state verb may be sent again."""
KEY_VERBS = frozenset({"press", "scroll"})
"""[V2.18] these may repeat after any effect but `none`, `unknown` or `frame_changed` alone."""
NO_PROGRESS_TOKENS = frozenset({"moved", "frame_changed"})


def same_action(earlier: ActionKey, later: ActionKey, present: frozenset[tuple[int, ...]]) -> bool:
    """[V2.11] decision 3: verb + params + RuntimeId; when the earlier RuntimeId is gone from the
    current snapshot (a re-render), the ledger identity (role, normalised name, automation id)."""
    if earlier.verb != later.verb or earlier.params != later.params:
        return False
    if earlier.runtime_id == later.runtime_id:
        return True
    return earlier.runtime_id not in present and earlier.identity == later.identity


def _effect_key(effect: EffectReport | None) -> tuple[frozenset[str], ...] | None:
    if effect is None:
        return None
    return (frozenset(effect.classes), frozenset(effect.tokens), frozenset(effect.unread))


def made_progress(verb: str, effect: EffectReport | None) -> bool:
    """[V2.18] whether a send's effect allows the same send again."""
    if effect is None or "unknown" in effect.classes:
        return False
    if verb in KEY_VERBS:
        return bool(effect.classes - {"none"}) or bool(effect.tokens - NO_PROGRESS_TOKENS)
    return bool(effect.classes & STRONG)


class Strikes:
    """Counts turns that tick no subgoal and produce no new state; any tick or new state resets
    the count. One strike per turn, however many reasons hold; every reason is recorded. At
    `limit` the task pauses."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._count = 0
        self._seen: set[str] = set()
        self._last: tuple[ActionKey, EffectReport | None] | None = None

    def record(self, turn: TurnFacts) -> tuple[StrikeReason, ...]:
        """Every reason this turn is a strike (empty: no strike, and the count resets)."""
        reasons: list[StrikeReason] = []
        if not turn.ticked:
            reasons = self._reasons(turn)
        self._seen.add(turn.state_digest)
        if turn.action is not None:
            self._last = (turn.action, turn.effect)
        self._count = self._count + 1 if reasons else 0
        return tuple(reasons)

    def _reasons(self, turn: TurnFacts) -> list[StrikeReason]:
        reasons: list[StrikeReason] = []
        classes = set() if turn.effect is None else turn.effect.classes
        if "none" in classes:
            reasons.append("effect_none")
        if "unknown" in classes:
            reasons.append("effect_unknown")
        if (
            turn.action is not None
            and self._last is not None
            and same_action(self._last[0], turn.action, turn.present)
            and _effect_key(self._last[1]) == _effect_key(turn.effect)
        ):
            reasons.append("same_action_same_effect")
        if turn.state_digest in self._seen:
            reasons.append("state_seen")
        if turn.claim_rejected:
            reasons.append("claim_rejected")
        if turn.refused:
            reasons.append("refused")
        return reasons

    @property
    def count(self) -> int:
        return self._count

    @property
    def paused(self) -> bool:
        return self._count >= self.limit

    def may_send(self, action: ActionKey, *, present: frozenset[tuple[int, ...]]) -> bool:
        """[V2.18] False for the same action as the last one sent, unless that send made
        progress: for `click`, `type`, `choose` and the state verbs a strong effect; for `press`
        and `scroll` any effect but `none`, `unknown` or `frame_changed` alone."""
        if self._last is None or not same_action(self._last[0], action, present):
            return True
        return made_progress(action.verb, self._last[1])
