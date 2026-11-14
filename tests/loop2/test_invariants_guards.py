"""The step 3 guards: budgets and no-progress strikes ([V2.4]; packet §3). Red on the 3c stub."""

from __future__ import annotations

import pytest

from steerai.loop2.guards import (
    ActionKey,
    BudgetToken,
    StrikeReason,
    Strikes,
    TurnFacts,
    Usage,
    budget_hit,
)
from steerai.loop2.task import Budgets
from steerai.world.effects import EffectClass
from tests.loop2.world_fakes import pending, report

BUDGETS = Budgets(max_actions=10, max_model_calls=5, max_cost_usd=0.5, max_wall_s=60.0)
CLICK: ActionKey = ("click", (9, 3), None)
OTHER: ActionKey = ("click", (9, 6), None)


def turn(
    *,
    ticked: bool = False,
    action: ActionKey | None = CLICK,
    classes: tuple[EffectClass, ...] = ("content_changed",),
    digest: str = "d1",
    rejected: bool = False,
) -> TurnFacts:
    effect = report(*classes) if classes else None
    return TurnFacts(
        ticked=ticked, action=action, effect=effect, state_digest=digest, claim_rejected=rejected
    )


# ---- budgets (§8.9) ---------------------------------------------------------------------------


@pending("budgets")
def test_within_every_budget_nothing_is_hit() -> None:
    assert budget_hit(BUDGETS, Usage(actions=9, model_calls=4, cost_usd=0.4, wall_s=59.0)) is None


@pending("budgets")
@pytest.mark.parametrize(
    ("used", "token"),
    [
        (Usage(actions=10, model_calls=0, cost_usd=0.0, wall_s=0.0), "max_actions"),
        (Usage(actions=0, model_calls=5, cost_usd=0.0, wall_s=0.0), "max_model_calls"),
        (Usage(actions=0, model_calls=0, cost_usd=0.5, wall_s=0.0), "max_cost"),
        (Usage(actions=0, model_calls=0, cost_usd=0.0, wall_s=60.0), "max_wall"),
    ],
    ids=["actions", "model_calls", "cost", "wall"],
)
def test_each_budget_reached_is_its_token(used: Usage, token: BudgetToken) -> None:
    assert budget_hit(BUDGETS, used) == token


# ---- strikes ([V2.4]) -------------------------------------------------------------------------


@pending("strikes")
@pytest.mark.parametrize(
    ("classes", "reason"), [(("none",), "effect_none"), (("unknown",), "effect_unknown")]
)
def test_a_none_or_unknown_effect_is_a_strike(
    classes: tuple[EffectClass, ...], reason: StrikeReason
) -> None:
    assert Strikes(limit=3).record(turn(classes=classes)) == reason


@pending("strikes")
def test_the_same_action_with_the_same_effect_is_a_strike() -> None:
    strikes = Strikes(limit=3)
    strikes.record(turn(digest="d1"))
    assert strikes.record(turn(digest="d2")) == "same_action_same_effect"


@pending("strikes")
def test_a_state_already_seen_is_a_strike() -> None:
    strikes = Strikes(limit=3)
    strikes.record(turn(action=CLICK, digest="d1"))
    strikes.record(turn(action=OTHER, digest="d2", classes=("revealed",)))
    assert strikes.record(turn(action=None, classes=(), digest="d1")) == "state_seen"


@pending("strikes")
def test_a_rejected_claim_is_a_strike() -> None:
    assert Strikes(limit=3).record(turn(action=None, classes=(), rejected=True)) == (
        "claim_rejected"
    )


@pending("strikes")
def test_a_tick_resets_the_count() -> None:
    strikes = Strikes(limit=2)
    strikes.record(turn(classes=("none",), digest="d1"))
    assert strikes.record(turn(ticked=True, action=OTHER, digest="d2")) is None
    strikes.record(turn(classes=("none",), digest="d3"))
    assert strikes.paused is False


@pending("strikes")
def test_the_limit_pauses_the_task() -> None:
    strikes = Strikes(limit=3)
    for digest in ("d1", "d2", "d3"):
        strikes.record(turn(classes=("none",), digest=digest))
    assert strikes.paused is True


@pending("strikes")
def test_the_same_action_is_never_sent_twice_in_a_row() -> None:
    strikes = Strikes(limit=3)
    strikes.record(turn(action=CLICK))
    assert strikes.may_send(CLICK) is False
    assert strikes.may_send(OTHER) is True
