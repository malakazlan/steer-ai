"""The deferred run_task edge cases (#326-#329): a kill during a decide, strike accounting on
mixed batches, a re-render between the decision and the fresh read, identity and digest, and
the budget and price shapes."""

from __future__ import annotations

import dataclasses
import math
from typing import Any

import pytest

from steerai.action.dispositions import Delivery
from steerai.loop2 import runner
from steerai.loop2.decision import Claim, Decision, SubgoalDoc, validate
from steerai.loop2.guards import ActionKey, same_action
from steerai.loop2.loop import action_risk as loop_action_risk
from steerai.loop2.runner import refusal_of, run_task
from steerai.loop2.task import Budgets, TokenPrice
from steerai.orchestrator.loop import REUSE_FRESHNESS_S
from steerai.world.effects import Observed, ValueFact
from steerai.world.elements import Element
from steerai.world.observe2 import FIRST_READY_S
from tests.loop2.world_fakes import (
    BAR,
    FIELD,
    GO,
    MENU,
    PAGE,
    PASSWORD,
    PRICE_READ_ON,
    PRICE_URL,
    QUERY_PAGE,
    SEND,
    TAB,
    URL,
    World,
    act,
    act_result,
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
    subs = [SubgoalDoc(id="s1", desc="tick", params={"text": "shop"}, closes="state", wanted="on")]
    box = element(42, "CheckBox", "Shop", (9, 42), 1)
    claim = Claim(subgoal="s1", kind="state", evidence=[box.id], param="text")
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
    assert (
        f"refused:secure_field:{PASSWORD.id}" in world.views[-1].history
    )  # [V2.47]: with the element's task handle
    assert all("refused" not in reasons for reasons in outcome.strikes)


def test_the_send_before_a_refusal_is_the_last_send_for_the_repeat_rule() -> None:
    """RT-26: the click went out with a weak effect, then the batch was refused; the same click
    next turn is a repeat ([V2.18])."""
    world = _mixed(decision(act("click", target=GO.id)))
    run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["click"]
    assert (
        f"refused:repeat_send:{GO.id}" in world.views[-1].history
    )  # [V2.47]: with the element's task handle


REBUILT = element(GO.id, "Button", "Go", (9, 333), 1)
"""GO re-rendered: the same role and name, a new RuntimeId."""
AGAIN = element(GO.id, "Button", "Go", (9, 334), 1)


def _shown(*elements: Element) -> Observed:
    swap = {e.id: e for e in elements}
    return observed([swap.get(e.id, e) for e in PAGE])


def test_a_target_re_rendered_after_the_decision_is_re_asked_not_refused() -> None:
    """RT-27, [V2.43] (2): the model named a listed element; it re-rendered before the fresh read.
    The world moved after what the model saw: re-observe and re-ask, no refusal, no strike."""
    world = World(
        [decision(act("click", target=GO.id), first=True)],
        observations=[observed(), _shown(REBUILT)],
    )
    outcome = run_task(task(), world.ports())
    history = world.views[1].history
    assert world.dispatched == []
    assert f"target_changed:{GO.id}" in history  # [V2.47]: with the element's task handle
    assert not any(t.startswith("refused:") for t in history)
    assert outcome.target_changes == 1
    assert outcome.stale_observations == 0  # counted apart from page-level staleness


def test_one_target_changed_then_a_good_turn_leaves_no_strike() -> None:
    """[V2.43] (2), the lead's first test."""
    world = World(
        [
            decision(act("click", target=GO.id), first=True),
            decision(act("click", target=MENU.id)),
            Decision(stop="cannot"),
        ],
        observations=[observed(), _shown(REBUILT), _shown(REBUILT), _shown(REBUILT)],
        effects=[report("content_changed")],
        later=observed([e for e in PAGE if e.id != SEND.id]),
    )
    outcome = run_task(task(), world.ports())
    assert [d.runtime_id for d in world.dispatched] == [MENU.runtime_id]
    assert outcome.strikes == ()


def test_two_target_changed_in_a_row_make_one_strike() -> None:
    """[V2.43] (2), the lead's second test: GO re-renders under both decisions."""
    world = World(
        [
            decision(act("click", target=GO.id), first=True),
            decision(act("click", target=10)),  # REBUILT's handle: the next free number
        ],
        observations=[observed(), _shown(REBUILT), _shown(REBUILT), _shown(AGAIN), _shown(AGAIN)],
    )
    outcome = run_task(task(), world.ports())
    assert world.views[1].handles[GO.id] == 10  # the second decision named the re-rendered GO
    assert world.dispatched == []
    assert outcome.target_changes == 2
    assert len(outcome.strikes) == 1


FOCUSED = observed(QUERY_PAGE, focus=FIELD.runtime_id)
ELSEWHERE = observed(QUERY_PAGE, focus=MENU.runtime_id)


def test_a_press_with_focus_listed_elsewhere_strikes_at_once() -> None:
    """[V2.43] (2), the lead's third test: the listing the model saw showed focus elsewhere."""
    world = World(
        [
            decision(act("type", target=FIELD.id, param="text"), first=True),
            decision(act("press", key="Enter")),
        ],
        effects=[report("own_value")],
        observations=[FOCUSED, FOCUSED, ELSEWHERE],
        after=ELSEWHERE,  # the type's after-read is what the press's listing shows
    )
    outcome = run_task(task(), world.ports())
    assert (
        f"refused:press_not_at_focus:{FIELD.id}" in world.views[-1].history
    )  # [V2.47]: with the element's task handle
    assert any("refused" in reasons for reasons in outcome.strikes)
    assert outcome.focus_losses == 0


def test_focus_lost_after_the_listing_showed_it_on_the_target_is_re_asked() -> None:
    """[V2.43] (2): the model saw focus on the target; it moved before the press. The world
    moved: re-observe and re-ask, no strike, counted on its own."""
    world = World(
        [
            decision(act("type", target=FIELD.id, param="text"), first=True),
            decision(act("press", key="Enter")),
        ],
        effects=[report("own_value")],
        observations=[FOCUSED, FOCUSED, ELSEWHERE, ELSEWHERE],  # listed on the field, then moved
    )
    outcome = run_task(task(), world.ports())
    assert (
        f"focus_lost:{FIELD.id}" in world.views[-1].history
    )  # [V2.47]: with the element's task handle
    assert not any("refused:press" in t for t in world.views[-1].history)
    assert outcome.focus_losses == 1
    assert all("refused" not in reasons for reasons in outcome.strikes)


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
    by_id = ActionKey(verb="click", params=(), runtime_id=(9, 74), identity=("Button", "", "btnGo"))
    moved = dataclasses.replace(by_id, runtime_id=(9, 75))
    assert same_action(by_id, moved, frozenset({(9, 75)}))  # an automation id alone identifies


def test_a_type_by_param_carries_its_resolved_text_and_the_outcome_never_does() -> None:
    """RT-36: the act port needs the text to type; the outcome keeps no text."""
    world = World([decision(TYPE, first=True)], effects=[report("own_value")], screen=FOCUSED)
    outcome = run_task(task(), world.ports())
    assert world.dispatched[0].text == "red shoes"
    assert all(d.text is None for d in outcome.dispatched)
    assert "red shoes" not in repr(world.dispatched[0])


# ---- #329: budget and price shapes (RT-32) ------------------------------------------------------

GOOD: dict[str, Any] = {  # Any: each cap's own type, mixed int and float
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
        Budgets(**(GOOD | {name: bad}))


@pytest.mark.parametrize("bad", [0.0, -0.01, math.nan, math.inf])
def test_a_token_price_must_be_finite_and_positive(bad: float) -> None:
    with pytest.raises(ValueError, match="price"):
        TokenPrice(
            usd_per_input_token=bad,
            usd_per_output_token=0.01,
            source=PRICE_URL,
            read_on=PRICE_READ_ON,
        )
    with pytest.raises(ValueError, match="price"):
        TokenPrice(
            usd_per_input_token=0.01,
            usd_per_output_token=bad,
            source=PRICE_URL,
            read_on=PRICE_READ_ON,
        )


def test_a_cost_cap_that_is_a_bool_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_cost_usd"):
        Budgets(**(GOOD | {"max_cost_usd": True}))


def test_a_priced_cost_cap_of_zero_pauses_before_the_first_call() -> None:
    price = TokenPrice(
        usd_per_input_token=0.01, usd_per_output_token=0.01, source=PRICE_URL, read_on=PRICE_READ_ON
    )
    budgets = Budgets(**(GOOD | {"max_cost_usd": 0.0}), price=price)
    world = World([decision(act("click", target=GO.id), first=True)])
    outcome = run_task(task(budgets), world.ports())
    assert (outcome.status, outcome.reason, outcome.model_calls) == ("paused", "max_cost", 0)


# ---- [V2.42] RT-29: the refusal kind by a fixed priority ----------------------------------------


@pytest.mark.parametrize(
    ("tokens", "kind", "token"),
    [
        (["id_not_listed", "not_in_step"], "step", "not_in_step"),
        (["not_in_step", "id_not_listed"], "step", "not_in_step"),
        (["id_not_listed", "not_delivered"], "world", "not_delivered"),
        (["not_delivered", "id_not_listed"], "world", "not_delivered"),
        (["not_delivered", "not_in_step", "secure_field"], "policy", "secure_field"),
        (["repeat_send", "id_not_listed"], "model", "repeat_send"),
    ],
)
def test_the_refusal_kind_is_by_priority_never_token_order(
    tokens: list[str], kind: str, token: str
) -> None:
    """[V2.42] RT-29: policy, then step limit, then world, then model fault."""
    assert refusal_of(tokens) == (kind, token)


def test_a_decision_refused_for_a_step_limit_and_a_model_fault_is_a_step_refusal() -> None:
    """The unlisted id comes first in `validate`'s tokens; the step limit still decides the
    status, and both tokens are recorded."""
    bad = Decision.model_validate(
        {
            "subgoals": [
                {
                    "id": "s1",
                    "desc": "x",
                    "params": {"text": "shop"},
                    "closes": "state",
                    "wanted": "on",
                }
            ],
            "actions": [
                {"verb": "click", "target": 999, "subgoal": "s1"},
                {"verb": "open", "subgoal": "s1"},
            ],
        }
    )
    world = World([bad, Decision(stop="cannot")])
    outcome = run_task(task(), world.ports())
    history = world.views[-1].history
    assert "refused:id_not_listed" in history
    assert "refused:not_in_step" in history
    assert (outcome.status, outcome.reason) == ("blocked", "not_in_step")


# ---- [V2.42] RT-30: a policy or step refusal stands until its subgoal is proven ----------------


def _refused_then_progress(*later: Decision) -> World:
    """s1: a type into the password field is refused (policy); then a click makes progress."""
    return World(
        [
            decision(act("type", target=PASSWORD.id, text="x"), first=True),
            decision(act("click", target=GO.id)),
            *later,
        ],
        effects=[report("content_changed")],
    )


def test_a_policy_refusal_stands_through_progress_elsewhere() -> None:
    world = _refused_then_progress(Decision(stop="cannot"))
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("blocked", "secure_field")


def test_a_policy_refusal_ends_when_its_subgoal_is_proven() -> None:
    """Two subgoals: s1's refusal stands until s1 is proven; then a stop with s2 open is the
    controller's own."""
    box = element(42, "CheckBox", "Shop", (9, 42), 1)
    subs = [
        SubgoalDoc(id="s1", desc="tick", params={"text": "shop"}, closes="state", wanted="on"),
        SubgoalDoc(id="s2", desc="find", params={"text": "red shoes"}, closes="page"),
    ]
    claim = Claim(subgoal="s1", kind="state", evidence=[box.id], param="text")
    world = World(
        [
            Decision(subgoals=subs, actions=[act("type", target=PASSWORD.id, text="x")]),
            Decision(claims=[claim]),
            Decision(stop="cannot"),
        ],
        screen=observed([*PAGE, box]),
    )
    world.states[(box.runtime_id, "on")] = True
    outcome = run_task(task(), world.ports())
    assert "proven:s1:claim" in world.views[-1].history
    assert (outcome.status, outcome.reason) == ("cannot", "controller_stop")


def test_a_world_refusal_still_ends_at_progress() -> None:
    """World and model faults keep "the latest since the last progress" ([V2.12])."""
    world = World(
        [
            decision(act("click", target=GO.id), first=True),
            decision(act("click", target=MENU.id)),
            Decision(stop="cannot"),
        ],
        results=[act_result("click", Delivery.NOT_DELIVERED)],
        effects=[report("content_changed")],
    )
    outcome = run_task(task(), world.ports())
    assert (
        f"refused:not_delivered:{GO.id}" in world.views[1].history
    )  # [V2.47]: with the element's task handle
    assert (outcome.status, outcome.reason) == ("cannot", "controller_stop")


# ---- [V2.42] RT-41: max_actions counts attempts --------------------------------------------------


def test_max_actions_counts_an_attempt_that_delivered_nothing() -> None:
    budgets = Budgets(**(GOOD | {"max_actions": 1}))
    world = World(
        [decision(act("click", target=GO.id), first=True), decision(act("click", target=MENU.id))],
        results=[act_result("click", Delivery.NOT_DELIVERED)],
    )
    outcome = run_task(task(budgets), world.ports())
    assert len(world.dispatched) == 1
    assert (outcome.status, outcome.reason) == ("paused", "max_actions")


# ---- [V2.42] RT-42: a declared commit on a verb that cannot commit -------------------------------

SUB1 = {"id": "s1", "desc": "x", "params": {"text": "shop"}, "closes": "state", "wanted": "on"}


@pytest.mark.parametrize(
    ("verb", "fields"),
    [("wait", {}), ("scroll", {"target": 3}), ("focus", {"target": 3})],
)
def test_a_commit_on_a_verb_that_cannot_commit_is_a_shape_error(
    verb: str, fields: dict[str, object]
) -> None:
    doc = {"verb": verb, "subgoal": "s1", "commit": "send", **fields}
    bad = Decision.model_validate({"subgoals": [SUB1], "actions": [doc]})
    tokens = validate(bad, listed_ids=frozenset({3}), goal="search the shop", first_turn=True)
    assert "commit_cannot_apply" in tokens
    assert refusal_of(tokens)[0] in {"model", "step"}


def test_a_commit_on_a_click_still_raises() -> None:
    doc = {"verb": "click", "subgoal": "s1", "commit": "send", "target": 3}
    ok = Decision.model_validate({"subgoals": [SUB1], "actions": [doc]})
    assert validate(ok, listed_ids=frozenset({3}), goal="search the shop", first_turn=True) == []


# ---- [V2.42] RT-19: an old resolving observation costs one read, never a re-ask ----------------


def test_an_old_observation_is_read_again_and_dispatched_when_nothing_moved() -> None:
    world = World([decision(act("click", target=GO.id), first=True)])
    world.observe_lag_s = REUSE_FRESHNESS_S + 1.0  # each read is that old by the time it is used
    outcome = run_task(task(), world.ports())
    assert [d.runtime_id for d in world.dispatched] == [GO.runtime_id]
    assert outcome.model_calls == 2  # the first decide, and the stop after the click: no re-ask
    assert outcome.stale_observations == 0


def test_an_old_observation_whose_page_moved_takes_the_staleness_rule() -> None:
    moved = dataclasses.replace(observed(), url=URL + "next", document_url=URL + "next")
    world = World(
        [decision(act("click", target=GO.id), first=True)],
        observations=[observed(), observed(), moved],
    )
    world.observe_lag_s = REUSE_FRESHNESS_S + 1.0
    outcome = run_task(task(), world.ports())
    assert world.dispatched == []
    assert outcome.stale_observations == 1


def test_a_fresh_enough_observation_is_not_read_again() -> None:
    world = World([decision(act("click", target=GO.id), first=True)])
    run_task(task(), world.ports())
    assert world.events[: world.events.index("act")].count("observe") == 2


# ---- [V2.42] RT-40: never call the model on an empty listing -------------------------------------


def test_an_empty_listing_never_reaches_the_model() -> None:
    """A window with nothing listable at all: a native one (no address bar); a browser frame
    with nothing listable is a page not ready ([V2.44] (2))."""
    native = dataclasses.replace(observed([]), url_observed=False, url=None)
    world = World([Decision(stop="cannot")], screen=native)
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == 0
    assert (outcome.status, outcome.reason) == ("cannot", "empty_listing")


def test_an_empty_listing_of_a_gone_window_is_infrastructure() -> None:
    gone = dataclasses.replace(observed([]), windows=(99,))  # the task's window is not listed
    world = World([Decision(stop="cannot")], screen=gone)
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == 0
    assert (outcome.status, outcome.reason) == ("stopped", "window_gone")


def test_a_step_limit_refusal_stands_through_progress_on_its_subgoal() -> None:
    """[V2.42] RT-30: the step-limit refusal attaches to the subgoal of the refused action (the
    `open`), and a later click's progress does not clear it. Intake packet rev 5 §4 P2: the
    refused first Decision freezes nothing, so the next turn re-declares the same subgoal."""
    subgoals = [
        {"id": "s1", "desc": "x", "params": {"text": "shop"}, "closes": "state", "wanted": "on"}
    ]
    first = Decision.model_validate(
        {"subgoals": subgoals, "actions": [{"verb": "open", "subgoal": "s1"}]}
    )
    again = Decision.model_validate(
        {"subgoals": subgoals, "actions": [{"verb": "click", "target": GO.id, "subgoal": "s1"}]}
    )
    world = World(
        [first, again, Decision(stop="cannot")],
        effects=[report("content_changed")],
    )
    outcome = run_task(task(), world.ports())
    assert [d.runtime_id for d in world.dispatched] == [GO.runtime_id]
    assert (outcome.status, outcome.reason) == ("blocked", "not_in_step")


def test_standing_refusals_take_the_rt29_order_before_the_latest() -> None:
    """[V2.42] RT-30: with several standing refusals, the RT-29 order first, then the latest: a
    policy refusal still decides after a later step-limit refusal on the same subgoal."""
    open_it = Decision.model_validate({"actions": [{"verb": "open", "subgoal": "s1"}]})
    world = World(
        [
            decision(act("type", target=PASSWORD.id, text="x"), first=True),
            open_it,
            Decision(stop="cannot"),
        ]
    )
    outcome = run_task(task(), world.ports())
    assert "refused:not_in_step" in world.views[-1].history
    assert (outcome.status, outcome.reason) == ("blocked", "secure_field")


# ---- principal re-review at e236066 ----------------------------------------------------------


def test_an_old_observation_is_rated_on_the_read_it_is_sent_against() -> None:
    """repro_risk: GO keeps its RuntimeId, role and name on the re-read, but its parent is now
    unread: R0 on the old read, R2 on the re-read. Refusal, risk, key and dispatch all use the
    read that is sent against, so it is never sent at R0."""
    moved_go = element(GO.id, "Button", "Go", GO.runtime_id, 99)  # parent no longer read
    reread = observed([moved_go if e.id == GO.id else e for e in PAGE])
    world = World(
        [decision(act("click", target=GO.id), first=True)],
        observations=[observed(), observed(), reread],
    )
    world.observe_lag_s = REUSE_FRESHNESS_S + 1.0
    outcome = run_task(task(), world.ports())
    assert world.dispatched == []
    assert (outcome.status, outcome.reason) == ("blocked", "commit_not_in_step")


def test_a_move_after_a_send_in_the_batch_is_scored_and_the_send_remembered() -> None:
    """repro_neutral: [type, press Enter], focus leaves the field after the type. The move ends
    the batch and is counted, but the turn is scored normally: the type is the last send, so
    the same type next turn is a repeat (RT-26). Staleness applies only when nothing was sent."""
    world = World(
        [
            decision(
                act("type", target=FIELD.id, param="text"), act("press", key="Enter"), first=True
            ),
            decision(act("focus", target=FIELD.id), act("type", target=FIELD.id, param="text")),
        ],
        effects=[report("own_value"), report("focus")],
        observations=[FOCUSED, FOCUSED],
        timeline=[(1, ELSEWHERE), (2, FOCUSED)],  # the focus click brings it back
    )
    outcome = run_task(task(), world.ports())
    # [V2.51] (3): the focus comes back first; the same type after it is still a repeat
    assert [d.action.verb for d in world.dispatched] == ["type", "focus"]
    assert (
        f"focus_lost:{FIELD.id}" in world.views[1].history
    )  # [V2.47]: with the element's task handle
    assert f"refused:repeat_send:{FIELD.id}" in world.views[-1].history  # [V2.47]: its handle
    assert outcome.focus_losses == 1


# ---- [V2.44] (2): a browser frame with no page content ends page_not_ready ---------------------

FRAME_ONLY = observed([TAB, BAR])
"""A browser window whose page has not appeared: the frame (tabs, address bar), no Document."""


def test_a_frame_with_no_document_ends_page_not_ready_never_reaching_the_model() -> None:
    """The first observation already waited out the first-ready deadline ([V2.31])."""
    world = World([Decision(stop="cannot")], screen=FRAME_ONLY)
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == 0
    assert "settle" not in world.events  # no second wait on top of the first-ready one
    assert (outcome.status, outcome.reason) == ("cannot", "page_not_ready")


def test_an_empty_document_ends_page_not_ready() -> None:
    world = World([Decision(stop="cannot")], screen=observed([PAGE[0], TAB, BAR]))
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == 0
    assert (outcome.status, outcome.reason) == ("cannot", "page_not_ready")


def test_a_native_window_with_no_document_is_a_normal_page() -> None:
    """No address bar (`url_observed` false): a native window, never a page not ready."""
    note = element(30, "Edit", "Text editor", (9, 30), None)
    native = dataclasses.replace(observed([note]), url_observed=False, url=None)
    world = World([Decision(stop="cannot")], screen=native)
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == 1


def test_a_page_that_blanks_mid_task_is_waited_for_until_it_returns() -> None:
    """Later in the task the page goes blank (a navigation under way): held, not ended, while
    the first-ready deadline lasts; the model is called again only on the page with content."""
    world = World(
        [decision(act("click", target=GO.id), first=True), Decision(stop="cannot")],
        effects=[report("navigated")],
        after=FRAME_ONLY,  # the watch's after is blank; the page then returns
    )
    world.settle_advance_s = 1.0
    outcome = run_task(task(), world.ports())
    assert "settle" in world.events
    assert outcome.model_calls == 2
    assert outcome.reason == "controller_stop"


def test_a_page_that_stays_blank_mid_task_ends_page_not_ready() -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True), Decision(stop="cannot")],
        effects=[report("navigated")],
        after=FRAME_ONLY,
        settled=FRAME_ONLY,  # every hold reads the blank page again
    )
    world.settle_advance_s = 1.0
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == 1
    assert (outcome.status, outcome.reason) == ("cannot", "page_not_ready")


def test_a_blank_page_whose_window_is_gone_is_infrastructure() -> None:
    gone = dataclasses.replace(FRAME_ONLY, windows=(99,))  # the task's window is not listed
    world = World([Decision(stop="cannot")], screen=gone)
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == 0
    assert (outcome.status, outcome.reason) == ("stopped", "window_gone")


# ---- the lead's [V2.43] confirmation ------------------------------------------------------------


def test_a_mixed_turn_leaves_the_stale_streak_as_it_was() -> None:
    """A stale turn (streak 1), then a mixed turn (a type is sent, then focus leaves the field
    before the press), then a stale turn again: the mixed turn neither raised nor reset the
    streak, so the second stale turn is the second in a row: a strike."""

    def at(url: str, focus: tuple[int, ...]) -> Observed:
        seen = observed(QUERY_PAGE, focus=focus)
        return dataclasses.replace(seen, url=url, document_url=url)

    a, b, c = URL + "a", URL + "b", URL + "c"
    world = World(
        [
            decision(act("click", target=GO.id), first=True),
            decision(  # the first decision was discarded as stale: this one takes the subgoals
                act("type", target=FIELD.id, param="text"), act("press", key="Enter"), first=True
            ),
            decision(act("click", target=GO.id)),
        ],
        effects=[report("own_value")],
        observations=[
            at(a, FIELD.runtime_id),
            at(b, FIELD.runtime_id),  # the page moved under the first decision: stale
            at(b, FIELD.runtime_id),
            at(c, FIELD.runtime_id),  # moved again under the third: stale
        ],
        after=at(b, MENU.runtime_id),  # after the type, focus is on the menu: a mixed turn
    )
    outcome = run_task(task(), world.ports())
    history = [t for v in world.views for t in v.history]
    assert f"focus_lost:{FIELD.id}" in history  # [V2.47]: with the element's task handle
    assert outcome.stale_observations == 2
    assert any("refused" in reasons for reasons in outcome.strikes)


def test_the_risk_is_rated_on_the_very_read_the_dispatch_is_sent_against(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RT-19, the lead's confirmation: one read serves the refusal, the risk, the key and the
    dispatch. The observation the risk is rated on is the same object the re-read returned,
    and the element rated is that read's own element."""
    rated: list[tuple[object, object]] = []
    real = loop_action_risk

    def spy(action: object, element: object, **kw: object) -> object:
        rated.append((kw["observed"], element))
        return real(action, element, **kw)  # type: ignore[arg-type]  # pass-through spy

    monkeypatch.setattr(runner, "action_risk", spy)
    reread = observed()
    world = World(
        [decision(act("click", target=GO.id), first=True)],
        observations=[observed(), observed(), reread],
    )
    world.observe_lag_s = REUSE_FRESHNESS_S + 1.0
    run_task(task(), world.ports())
    assert len(world.dispatched) == 1
    rated_on, element = rated[0]
    assert rated_on is world.read_log[2]
    assert any(e is element for e in world.read_log[2].snapshot.elements)


def test_a_press_re_read_on_age_rechecks_the_focus_on_the_new_read() -> None:
    """RT-19 for a press: the loop itself re-checks focus on the re-read, without relying on the
    act port. Focus moved to the menu by the re-read: a world move (`focus_lost`), no press."""
    world = World(
        [
            decision(act("type", target=FIELD.id, param="text"), first=True),
            decision(act("press", key="Enter")),
        ],
        effects=[report("own_value")],
        observations=[FOCUSED] * 5 + [ELSEWHERE] * 4,
    )
    world.observe_lag_s = REUSE_FRESHNESS_S + 1.0
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type"]
    assert outcome.focus_losses == 1


# ---- #342 review ---------------------------------------------------------------------------------


def test_a_blank_page_never_ends_before_the_first_ready_deadline() -> None:
    """Short holds (0.1 s) never end the wait early: it ends on `FIRST_READY_S` after the page
    went blank, not on a count of holds."""
    world = World(
        [decision(act("click", target=GO.id), first=True)],
        effects=[report("navigated")],
        after=FRAME_ONLY,
        settled=FRAME_ONLY,
    )
    world.settle_advance_s = 0.1
    outcome = run_task(task(), world.ports())
    assert outcome.reason == "page_not_ready"
    assert world.now >= FIRST_READY_S - 1e-6


def test_a_clock_that_does_not_move_ends_the_wait_after_one_hold() -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True)],
        effects=[report("navigated")],
        after=FRAME_ONLY,
        settled=FRAME_ONLY,
    )
    outcome = run_task(task(), world.ports())
    assert outcome.reason == "page_not_ready"
    assert world.events.count("settle") == 1


def test_a_browser_frame_with_nothing_listable_is_a_page_not_ready() -> None:
    """[V2.44] (2): a frame-only browser page is never `empty_listing`, even with nothing
    listable at all."""
    blank = dataclasses.replace(observed([]), url_observed=True)
    world = World([Decision(stop="cannot")], screen=blank)
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == 0
    assert (outcome.status, outcome.reason) == ("cannot", "page_not_ready")


def test_several_empty_root_documents_are_not_blank() -> None:
    """Unreadable, not blank: no single top Document, so the page goes to the model as before."""
    roots = [
        element(1, "Document", "A", (9, 1), None),
        element(2, "Document", "B", (9, 2), None),
        TAB,
        BAR,
    ]
    world = World([Decision(stop="cannot")], screen=observed(roots))
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == 1


def test_the_deadline_counts_from_when_the_page_went_blank() -> None:
    """The page goes blank 20 s into the task: it is still waited for `FIRST_READY_S` from
    then, never from the task's start."""
    world = World(
        [decision(act("click", target=GO.id), first=True)],
        effects=[report("navigated")],
        later=FRAME_ONLY,  # the screen stays blank after the click, on every read
        settled=FRAME_ONLY,
    )
    world.watch_s = 20.0
    world.settle_advance_s = 0.5
    outcome = run_task(task(), world.ports())
    assert outcome.reason == "page_not_ready"
    assert world.now >= 20.0 + FIRST_READY_S - 1e-6


def test_a_wall_budget_that_ends_the_blank_page_wait_is_max_wall() -> None:
    """The wall budget runs out during the hold, before the first-ready deadline: the task pauses
    on the budget (`max_wall`), never `page_not_ready`."""
    budgets = Budgets(**(GOOD | {"max_wall_s": 3.0}))
    world = World(
        [decision(act("click", target=GO.id), first=True), Decision(stop="cannot")],
        effects=[report("navigated")],
        after=FRAME_ONLY,
        settled=FRAME_ONLY,
    )
    world.settle_advance_s = 0.5
    outcome = run_task(task(budgets), world.ports())
    assert (outcome.status, outcome.reason) == ("paused", "max_wall")
