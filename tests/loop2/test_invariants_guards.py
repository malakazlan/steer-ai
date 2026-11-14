"""The step 3 guards: budgets and no-progress strikes ([V2.4], [V2.11] decisions 3 and 4; packet
§3). Red on the 3c stub."""

from __future__ import annotations

import dataclasses

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
GO_RID, MENU_RID, GO_AGAIN_RID = (9, 3), (9, 6), (9, 33)
CLICK = ActionKey(verb="click", params=(), runtime_id=GO_RID, identity=("Button", "go", ""))
OTHER = ActionKey(verb="click", params=(), runtime_id=MENU_RID, identity=("Button", "options", ""))
RERENDERED = ActionKey(
    verb="click", params=(), runtime_id=GO_AGAIN_RID, identity=("Button", "go", "")
)
"""The same button after a re-render: a new RuntimeId, the same ledger identity."""
PRESENT = frozenset({GO_RID, MENU_RID})


def turn(
    *,
    ticked: bool = False,
    action: ActionKey | None = CLICK,
    classes: tuple[EffectClass, ...] = ("content_changed",),
    digest: str = "d1",
    rejected: bool = False,
    present: frozenset[tuple[int, ...]] = PRESENT,
) -> TurnFacts:
    effect = report(*classes) if classes else None
    return TurnFacts(
        ticked=ticked,
        action=action,
        effect=effect,
        state_digest=digest,
        claim_rejected=rejected,
        present=present,
    )


# ---- budgets (§8.9) ---------------------------------------------------------------------------


@pending("§8.9 budgets")
def test_within_every_budget_nothing_is_hit() -> None:
    assert budget_hit(BUDGETS, Usage(actions=9, model_calls=4, cost_usd=0.4, wall_s=59.0)) is None


@pending("§8.9 budgets")
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


# ---- strikes ([V2.4], [V2.11].3) --------------------------------------------------------------


@pending("[V2.4] strikes")
@pytest.mark.parametrize(
    ("classes", "reason"), [(("none",), "effect_none"), (("unknown",), "effect_unknown")]
)
def test_a_none_or_unknown_effect_is_a_strike(
    classes: tuple[EffectClass, ...], reason: StrikeReason
) -> None:
    assert reason in Strikes(limit=3).record(turn(classes=classes))


@pending("[V2.4] strikes")
def test_the_same_action_with_the_same_effect_is_a_strike() -> None:
    strikes = Strikes(limit=3)
    strikes.record(turn(digest="d1"))
    assert "same_action_same_effect" in strikes.record(turn(digest="d2"))


@pending("[V2.11].3 strikes: same action by ledger identity after a re-render")
def test_a_rerendered_target_is_the_same_action_by_its_ledger_identity() -> None:
    strikes = Strikes(limit=3)
    strikes.record(turn(action=CLICK, digest="d1"))
    after_rerender = frozenset({GO_AGAIN_RID, MENU_RID})
    reasons = strikes.record(turn(action=RERENDERED, digest="d2", present=after_rerender))
    assert "same_action_same_effect" in reasons


@pending("[V2.11].3 strikes: same action is verb + params + RuntimeId")
def test_a_new_runtime_id_while_the_old_one_is_still_present_is_another_action() -> None:
    strikes = Strikes(limit=3)
    strikes.record(turn(action=CLICK, digest="d1"))
    both = frozenset({GO_RID, GO_AGAIN_RID, MENU_RID})
    reasons = strikes.record(turn(action=RERENDERED, digest="d2", present=both))
    assert "same_action_same_effect" not in reasons


@pending("[V2.11].3 strikes: same action is verb + params + RuntimeId")
def test_other_params_on_the_same_element_are_another_action() -> None:
    strikes = Strikes(limit=3)
    enter = ActionKey(verb="press", params=("Enter",), runtime_id=GO_RID, identity=CLICK.identity)
    tab = ActionKey(verb="press", params=("Tab",), runtime_id=GO_RID, identity=CLICK.identity)
    strikes.record(turn(action=enter, digest="d1"))
    assert "same_action_same_effect" not in strikes.record(turn(action=tab, digest="d2"))


@pending("[V2.4] strikes")
def test_a_state_already_seen_is_a_strike() -> None:
    strikes = Strikes(limit=3)
    strikes.record(turn(action=CLICK, digest="d1"))
    strikes.record(turn(action=OTHER, digest="d2", classes=("revealed",)))
    assert "state_seen" in strikes.record(turn(action=None, classes=(), digest="d1"))


@pending("[V2.4] strikes")
def test_a_rejected_claim_is_a_strike() -> None:
    reasons = Strikes(limit=3).record(turn(action=None, classes=(), rejected=True))
    assert "claim_rejected" in reasons


@pending("[V2.11].4 a refusal is a strike")
def test_a_refused_turn_is_a_strike() -> None:
    refused = dataclasses.replace(turn(action=None, classes=()), refused=True)
    assert "refused" in Strikes(limit=3).record(refused)


@pending("[V2.11].3 one strike per turn, every reason recorded")
def test_several_reasons_in_one_turn_are_one_strike_with_every_reason() -> None:
    strikes = Strikes(limit=3)
    strikes.record(turn(action=OTHER, digest="d1", classes=("revealed",)))
    reasons = strikes.record(turn(action=None, classes=("none",), digest="d1", rejected=True))
    assert {"effect_none", "state_seen", "claim_rejected"} <= set(reasons)
    assert strikes.count == 1


@pending("[V2.4] strikes")
def test_a_tick_resets_the_count() -> None:
    strikes = Strikes(limit=2)
    strikes.record(turn(classes=("none",), digest="d1"))
    assert strikes.record(turn(ticked=True, action=OTHER, digest="d2")) == ()
    assert strikes.count == 0
    strikes.record(turn(classes=("none",), digest="d3"))
    assert strikes.paused is False


@pending("[V2.4] strikes")
def test_the_limit_pauses_the_task() -> None:
    strikes = Strikes(limit=3)
    for digest in ("d1", "d2", "d3"):
        strikes.record(turn(classes=("none",), digest=digest))
    assert strikes.paused is True


@pending("[V2.4] never the same action twice in a row")
def test_the_same_action_is_never_sent_twice_in_a_row() -> None:
    strikes = Strikes(limit=3)
    strikes.record(turn(action=CLICK))
    assert strikes.may_send(CLICK, present=PRESENT) is False
    assert strikes.may_send(OTHER, present=PRESENT) is True


@pending("[V2.11].3 never the same action twice, by ledger identity after a re-render")
def test_a_rerendered_target_is_not_sent_again_in_a_row() -> None:
    strikes = Strikes(limit=3)
    strikes.record(turn(action=CLICK))
    assert strikes.may_send(RERENDERED, present=frozenset({GO_AGAIN_RID})) is False
