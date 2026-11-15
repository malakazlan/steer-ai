"""[V2.69] Two, intake packet rev 5 §4 and its note (coordinator ruling, 2026-11-15): when the
strike limit ends a task and every strike has the same single cause, the reason names it
(`no_progress:<token>`); otherwise plain `no_progress`."""

from __future__ import annotations

import pytest

from steerai.loop2.decision import Decision
from steerai.loop2.guards import StrikeReason, single_cause
from steerai.loop2.loop import Reason, render_reason
from steerai.loop2.runner import run_task
from tests.loop2.world_fakes import GO, World, act, decision, report, task

ONE = frozenset({"not_in_step"})
REFUSED: tuple[StrikeReason, ...] = ("refused",)
SEEN: tuple[StrikeReason, ...] = ("state_seen", "refused")


def test_one_token_behind_every_refusal_strike_is_the_cause() -> None:
    assert single_cause([(REFUSED, ONE), (SEEN, ONE), (SEEN, ONE)]) == "not_in_step"


@pytest.mark.parametrize(
    "strikes",
    [
        [],
        [(REFUSED, ONE), (SEEN, frozenset({"target_gone"}))],
        [(REFUSED, frozenset({"not_in_step", "target_gone"}))],
        [(REFUSED, ONE), (("effect_none",), frozenset())],
        [(REFUSED, ONE), (("claim_rejected", "refused"), ONE)],
        [(REFUSED, ONE), (("same_action_same_effect", "state_seen"), frozenset())],
        [(("state_seen",), ONE)],
    ],
    ids=[
        "none",
        "two_causes",
        "two_tokens",
        "effect_none",
        "claim_rejected",
        "same_action",
        "no_refusal",
    ],
)
def test_anything_else_is_mixed(
    strikes: list[tuple[tuple[StrikeReason, ...], frozenset[str]]],
) -> None:
    assert single_cause(strikes) is None


def test_a_detail_renders_after_its_class_and_stays_closed() -> None:
    assert render_reason(Reason("no_progress", detail="intake_insufficient")) == (
        "no_progress:intake_insufficient"
    )
    assert render_reason(Reason("no_progress")) == "no_progress"
    with pytest.raises(ValueError, match="reason_detail"):
        Reason("no_progress", detail="Red Shoes")


def test_identical_refusals_through_the_limit_name_their_cause() -> None:
    """The same refused click every turn: each strike's one token is `not_in_step`."""
    subgoal = {"id": "s1", "desc": "x", "params": {"text": "shop"}, "closes": "page"}
    opener = Decision.model_validate(
        {"subgoals": [subgoal], "actions": [{"verb": "open", "subgoal": "s1"}]}
    )
    world = World([opener] * 5)
    outcome = run_task(task(), world.ports())
    assert world.dispatched == []
    assert (outcome.status, outcome.reason) == ("paused", "no_progress:not_in_step")


def test_a_run_with_any_other_strike_stays_plain() -> None:
    first = decision(act("click", target=GO.id), first=True)
    later = [decision(act("click", target=GO.id))] * 4
    world = World([first, *later], effects=[report("none")] * 5)
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("paused", "no_progress")
