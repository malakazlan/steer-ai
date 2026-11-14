"""The deferred run_task edge cases (#326-#329): a kill during a decide, strike accounting on
mixed batches, a re-render between the decision and the fresh read, identity and digest, and
the budget and price shapes."""

from __future__ import annotations

import dataclasses
import math

import pytest

from steerai.loop2.decision import Claim, Decision, SubgoalDoc
from steerai.loop2.guards import ActionKey, same_action
from steerai.loop2.runner import run_task
from steerai.loop2.task import Budgets, TokenPrice
from steerai.world.effects import ValueFact
from tests.loop2.world_fakes import (
    FIELD,
    GO,
    MENU,
    PAGE,
    PASSWORD,
    QUERY_PAGE,
    World,
    act,
    decision,
    element,
    observed,
    report,
    task,
)

# ---- #326: a kill while the decide port waits on the model -------------------------------------


def test_a_decision_returning_after_the_kill_is_discarded() -> None:
    """[V2.40] Q4: the kill arrives during the controller call; the Decision that comes back
    (here a stop) is never acted on: the task ends stopped with the kill's token."""
    world = World([Decision(stop="cannot")], interrupt_in_decide=1, interruption="killed")
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "killed")


def test_a_claim_returning_after_the_kill_never_closes_the_task() -> None:
    subs = [SubgoalDoc(id="s1", desc="tick", params={"text": "shop"}, closes="state")]
    box = element(42, "CheckBox", "Shop", (9, 42), 1)
    claim = Claim(subgoal="s1", kind="state", evidence=[box.id], param="text", wanted="on")
    world = World(
        [Decision(subgoals=subs, claims=[claim])],
        screen=observed([*PAGE, box]),
        interrupt_in_decide=1,
        interruption="killed",
    )
    world.states[(box.runtime_id, "on")] = True
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "killed")
    assert world.dispatched == []


# ---- #327: strike accounting on mixed batches (RT-25/26), the mid-turn re-render (RT-27) --------

MOVED = observed([e for e in PAGE if e.id != MENU.id])
"""The page after the first action: something changed, so the turn is new state."""


def _mixed(*later: Decision) -> World:
    """Turn 1: a click with its own effect, then a type into the password field (refused by
    policy) in the same batch."""
    first = decision(
        act("click", target=GO.id), act("type", target=PASSWORD.id, text="x"), first=True
    )
    return World([first, *later], effects=[report("own_state")], later=MOVED)


def test_a_turn_that_dispatched_before_a_refusal_is_no_refused_strike() -> None:
    """RT-25: a refusal is a strike only when nothing was dispatched ([V2.11] decision 4)."""
    world = _mixed()
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["click"]
    assert "refused:secure_field" in world.views[-1].history
    assert all("refused" not in reasons for reasons in outcome.strikes)


def test_the_send_before_a_refusal_is_the_last_send_for_the_repeat_rule() -> None:
    """RT-26: the click went out with a weak effect, then the batch was refused; the same click
    next turn is a repeat ([V2.18])."""
    world = _mixed(decision(act("click", target=GO.id)))
    run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["click"]
    assert "refused:repeat_send" in world.views[-1].history


def test_a_target_re_rendered_after_the_decision_is_a_world_refusal() -> None:
    """RT-27: the model named a listed element; it was re-rendered (a new RuntimeId) before the
    fresh read. That is the world moving, not a model fault."""
    rebuilt = element(GO.id, "Button", "Go", (9, 333), 1)
    fresh = observed([rebuilt if e.id == GO.id else e for e in PAGE])
    world = World(
        [decision(act("click", target=GO.id), first=True)], observations=[observed(), fresh]
    )
    run_task(task(), world.ports())
    history = world.views[-1].history
    assert world.dispatched == []
    assert "refused:target_changed" in history
    assert "refused:id_not_listed" not in history


# ---- #328: identity and digest (RT-33/34/35/36) -----------------------------------------------

TYPE = act("type", target=FIELD.id, param="text")
FOCUSED = observed(QUERY_PAGE, focus=FIELD.runtime_id)


def test_the_pending_digest_is_what_steer_typed_not_what_the_page_completed() -> None:
    """RT-33: the page completed the text after the type; the field no longer holds what Steer
    typed, so a later submit is never a query."""
    world = World(
        [decision(TYPE, first=True), decision(act("click", target=GO.id))],
        effects=[report("own_value")],
        screen=FOCUSED,
    )
    world.autocomplete[FIELD.runtime_id] = ValueFact(filled=True, digest="completed")
    run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type"]


def test_two_different_free_texts_into_one_field_are_two_actions() -> None:
    """RT-34: the repeat rule keys a free-text type by its text's digest, never the raw text."""
    world = World(
        [
            decision(act("type", target=FIELD.id, text="first words"), first=True),
            decision(act("type", target=FIELD.id, text="other words")),
        ],
        effects=[report("own_value"), report("own_value")],
        screen=FOCUSED,
    )
    world.fact_text = True
    run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type", "type"]


def test_two_unnamed_buttons_are_never_the_same_action() -> None:
    """RT-35: an identity with no name and no automation id identifies nothing; a re-render of
    one unnamed button never makes another one the same action."""
    gone = ActionKey(verb="click", params=(), runtime_id=(9, 70), identity=("Button", "", ""))
    other = ActionKey(verb="click", params=(), runtime_id=(9, 71), identity=("Button", "", ""))
    assert not same_action(gone, other, frozenset({(9, 71)}))
    named = ActionKey(verb="click", params=(), runtime_id=(9, 72), identity=("Button", "go", ""))
    again = dataclasses.replace(named, runtime_id=(9, 73))
    assert same_action(named, again, frozenset({(9, 73)}))  # a named re-render still matches


def test_a_type_by_param_carries_its_resolved_text_and_the_outcome_never_does() -> None:
    """RT-36: the act port needs the text to type; the outcome keeps no text."""
    world = World([decision(TYPE, first=True)], effects=[report("own_value")], screen=FOCUSED)
    outcome = run_task(task(), world.ports())
    assert world.dispatched[0].text == "red shoes"
    assert all(d.text is None for d in outcome.dispatched)
    assert "red shoes" not in repr(world.dispatched[0])


# ---- #329: budget and price shapes (RT-32) ------------------------------------------------------

GOOD = {
    "max_actions": 10,
    "max_model_calls": 5,
    "max_tokens": 10_000,
    "max_cost_usd": 1.0,
    "max_wall_s": 60.0,
}


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("max_wall_s", math.inf),
        ("max_wall_s", math.nan),
        ("max_cost_usd", -1.0),
        ("max_cost_usd", math.inf),
        ("max_actions", 0),
        ("max_model_calls", -1),
        ("max_tokens", True),
        ("max_actions", 2.5),
    ],
)
def test_a_budget_cap_must_be_finite_and_positive(name: str, bad: object) -> None:
    with pytest.raises(ValueError, match=name):
        Budgets(**(GOOD | {name: bad}))  # type: ignore[arg-type]  # the bad value is the point


@pytest.mark.parametrize("bad", [0.0, -0.01, math.nan, math.inf])
def test_a_token_price_must_be_finite_and_positive(bad: float) -> None:
    with pytest.raises(ValueError, match="price"):
        TokenPrice(usd_per_input_token=bad, usd_per_output_token=0.01)
    with pytest.raises(ValueError, match="price"):
        TokenPrice(usd_per_input_token=0.01, usd_per_output_token=bad)
