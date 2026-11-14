"""The #320 review round (principal REVISE + edge-case hunter, rt_findings.md): one test per
finding fixed in `run_task`, written before the fix. Ids are the review's."""

from __future__ import annotations

import dataclasses

import pytest

from steerai.action.dispositions import ActionResult, Delivery
from steerai.errors import ErrorCode, SteerError
from steerai.loop2 import runner
from steerai.loop2._stub import PENDING
from steerai.loop2.controller import SYSTEM_RULES
from steerai.loop2.decision import Claim, Decision, SubgoalDoc
from steerai.loop2.loop import ActResult, TokenUsage
from steerai.loop2.runner import run_task
from steerai.loop2.task import Budgets, TokenPrice
from steerai.world.effects import ValueFact, poll_tier
from steerai.world.elements import Element
from tests.loop2.world_fakes import (
    FIELD,
    GO,
    PAGE,
    QUERY_PAGE,
    TYPED_VALUE,
    World,
    act,
    act_result,
    decision,
    element,
    observed,
    report,
    snapshot,
    task,
)

TYPE = act("type", target=FIELD.id, param="text")


def _history(world: World) -> list[str]:
    return [t for view in world.views for t in view.history]


def test_p1_an_uncertain_type_still_holds_the_submit_rule() -> None:
    """P1/RT-02: a type that may have sent its text is pending, read as unread (fails closed),
    so a click beside the field is a submit, never a plain click."""
    world = World(
        [decision(TYPE, first=True), decision(act("click", target=GO.id))],
        results=[act_result("type", Delivery.DELIVERY_UNCERTAIN)],
        screen=observed(QUERY_PAGE, focus=FIELD.runtime_id),
    )
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type"]
    assert (outcome.status, outcome.reason) == ("blocked", "commit_not_in_step")


def test_p4_a_press_whose_target_left_the_snapshot_is_refused_with_a_token() -> None:
    """P4/RT-14: the focus still reads the old RuntimeId, but the element is gone."""
    gone = [e for e in QUERY_PAGE if e.id != FIELD.id]
    world = World(
        [decision(act("click", target=FIELD.id), first=True), decision(act("press", key="Enter"))],
        effects=[report("focus")],
        screen=observed(QUERY_PAGE, focus=FIELD.runtime_id),
        later=observed(gone, focus=FIELD.runtime_id),
    )
    run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["click"]
    assert "refused:press_target_gone" in _history(world)


def test_p4_a_press_whose_target_changed_role_or_name_is_refused() -> None:
    renamed = [e if e.id != FIELD.id else dataclasses.replace(e, name="Delete") for e in QUERY_PAGE]
    world = World(
        [decision(act("click", target=FIELD.id), first=True), decision(act("press", key="Enter"))],
        effects=[report("focus")],
        screen=observed(QUERY_PAGE, focus=FIELD.runtime_id),
        later=observed(renamed, focus=FIELD.runtime_id),
    )
    run_task(task(), world.ports())
    assert "refused:press_target_changed" in _history(world)


def test_rt04_a_dispatch_carries_the_role_and_name_the_act_port_re_checks() -> None:
    world = World([decision(act("click", target=GO.id), first=True)])
    run_task(task(), world.ports())
    assert [(d.role, d.name) for d in world.dispatched] == [(GO.role, GO.name)]


def test_rt04_rt05_the_staleness_rule_and_the_enter_table_keep_loop2_not_ready() -> None:
    """PR C's two pieces are stubs, so `ready()` cannot turn True without them."""
    assert {"steerai.loop2.runner.stale_observation", "steerai.loop2.runner.enter_outcome"} <= (
        PENDING
    )


def test_rt06_a_commit_subgoal_is_never_ticked_in_step_3(monkeypatch: pytest.MonkeyPatch) -> None:
    """§8.8: a standard proof never closes a commit subgoal; in step 3 nothing does."""
    monkeypatch.setattr(runner, "check_claim", lambda *_a, **_k: ())
    send = [
        SubgoalDoc(id="s1", desc="send", params={"text": "red shoes"}, commit="send", closes="page")
    ]
    claim = Claim(subgoal="s1", kind="state", evidence=[GO.id], wanted="on")
    world = World([Decision(subgoals=send, claims=[claim])])
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"
    assert "claim:s1:standard_only" in _history(world)


def test_rt07_the_same_send_twice_in_one_batch_after_a_weak_effect_is_refused() -> None:
    world = World(
        [decision(act("click", target=GO.id), act("click", target=GO.id), first=True)],
        effects=[report("own_state")],
    )
    run_task(task(), world.ports())
    assert len(world.dispatched) == 1
    assert "refused:repeat_send" in _history(world)


def test_rt09_the_state_digest_reads_the_states_not_only_the_names() -> None:
    box = PAGE[2]
    on = observed([*PAGE[:2], dataclasses.replace(box, caps=("toggle:on",)), *PAGE[3:]])
    off = observed([*PAGE[:2], dataclasses.replace(box, caps=("toggle:off",)), *PAGE[3:]])
    assert runner._digest(on) != runner._digest(off)


def test_rt10_an_observation_from_another_clock_domain_stops_the_task() -> None:
    """One clock: `Snapshot.taken_at` and `Ports.clock` must share a domain, or the reuse age
    between batch actions is meaningless; the loop refuses to run on a mismatch."""
    world = World([decision(act("click", target=GO.id), first=True)])
    world.now = 1.7e9  # a wall clock, against the fakes' taken_at of 0
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "clock_domain")
    assert world.dispatched == []


def test_rt13_a_window_swap_mid_batch_ends_the_batch_never_raises() -> None:
    other = dataclasses.replace(observed(), snapshot=dataclasses.replace(snapshot(PAGE), hwnd=0x20))
    world = World(
        [decision(act("click", target=GO.id), act("click", target=PAGE[5].id), first=True)],
        effects=[report("own_state")],
        after=other,
    )
    run_task(task(), world.ports())
    assert len(world.dispatched) == 1


def test_rt15_a_kill_before_the_local_retry_stops_it() -> None:
    missed = ActResult(
        ActionResult(
            verb="click", delivery=Delivery.NOT_DELIVERED, code=ErrorCode.ELEMENT_NOT_FOUND
        ),
        read_back=None,
    )
    world = World(
        [decision(act("click", target=GO.id), first=True)],
        results=[missed],
        interrupt_after=1,
        interruption="killed",
    )
    outcome = run_task(task(), world.ports())
    assert len(world.dispatched) == 1, "no retry after the kill"
    assert (outcome.status, outcome.reason) == ("stopped", "killed")


def test_rt16_a_port_error_ends_the_task_with_a_token_and_keeps_the_evidence() -> None:
    world = World([decision(act("click", target=GO.id), first=True), decision(act("wait"))])
    ports = world.ports()
    calls = {"n": 0}

    def flaky() -> object:
        calls["n"] += 1
        if calls["n"] > 1:
            raise SteerError(ErrorCode.PERCEPTION_TIMEOUT, "read timed out")
        return world.screen

    ports = dataclasses.replace(ports, observe=flaky)  # type: ignore[arg-type]  # a raising fake
    world.after = dataclasses.replace(observed(), snapshot=snapshot(PAGE, stable=False))
    outcome = run_task(task(), ports)
    assert outcome.status == "stopped"
    assert outcome.reason == "port_perception_timeout"
    assert len(outcome.dispatched) == 1


def test_rt18_the_outcome_never_holds_typed_text() -> None:
    world = World([decision(act("type", target=FIELD.id, text="red shoes"), first=True)])
    outcome = run_task(task(), world.ports())
    assert world.dispatched[0].action.text == "red shoes", "the act port gets the text"
    assert [d.action.text for d in outcome.dispatched] == [None]
    assert [d.provenance for d in outcome.dispatched] == ["generated"]


def test_rt31_a_rejected_claim_is_a_strike_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "check_claim", lambda *_a, **_k: ("value_mismatch",))
    claim = Claim(subgoal="s1", kind="value", evidence=[FIELD.id], param="text")
    world = World([decision(first=True, claims=[claim])])
    outcome = run_task(task(), world.ports())
    assert any("claim_rejected" in strike for strike in outcome.strikes)


def test_rt37_a_malformed_interrupt_token_still_stops_the_task() -> None:
    world = World([decision(act("click", target=GO.id), first=True)], interrupt_after=0)
    world.interruption = "Take Over!"
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "interrupted")


def test_the_typed_field_is_read_once_before_its_enter() -> None:
    """Non-blocking ([V2.29] Q2): one read before dispatch serves the watch and the pending text."""
    world = World(
        [decision(TYPE, act("press", key="Enter"), first=True)],
        effects=[report("own_value"), report("none")],
        screen=observed(QUERY_PAGE, focus=FIELD.runtime_id),
    )
    run_task(task(), world.ports())
    acts = [i for i, e in enumerate(world.events) if e == "act"]
    assert world.events[acts[0] + 1 : acts[1]].count("read") <= 2, world.events
    between = world.events[acts[0] : acts[1]]
    assert between.count("read") >= 1


# ---- [V2.37] ------------------------------------------------------------------------------------

SECURE = act("type", target=PAGE[4].id, param="text")


def test_p5_policy_then_world_then_stop_ends_on_the_world_token() -> None:
    world = World(
        [
            decision(SECURE, first=True),
            decision(act("click", target=GO.id)),
            Decision(stop="cannot"),
        ],
        results=[act_result("click", Delivery.NOT_DELIVERED)],
    )
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("cannot", "after_refusal:not_delivered")


def test_p5_world_then_policy_then_stop_ends_blocked() -> None:
    world = World(
        [
            decision(act("click", target=GO.id), first=True),
            decision(SECURE),
            Decision(stop="cannot"),
        ],
        results=[act_result("click", Delivery.NOT_DELIVERED)],
    )
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("blocked", "secure_field")


def test_p2_pending_text_belongs_to_the_field_whatever_subgoal_typed_it() -> None:
    """A click beside a field typed under s1 is a submit even when s2 clicks."""
    two = [
        SubgoalDoc(id="s1", desc="type", params={"text": "red shoes"}, closes="page"),
        SubgoalDoc(id="s2", desc="go", closes="state"),
    ]
    plain = observed(list(QUERY_PAGE), focus=FIELD.runtime_id, searchbox=False)
    world = World(
        [
            Decision(subgoals=two, actions=[TYPE]),
            decision(act("click", target=GO.id, subgoal="s2")),
        ],
        effects=[report("own_value")],
        screen=plain,
    )
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type"]
    assert (outcome.status, outcome.reason) == ("blocked", "commit_not_in_step")


def test_p6_the_token_budget_pauses() -> None:
    budgets = Budgets(
        max_actions=20, max_model_calls=10, max_tokens=150, max_cost_usd=1.0, max_wall_s=60.0
    )
    world = World([decision(act("click", target=GO.id), first=True), decision(act("wait"))])
    outcome = run_task(task(budgets), world.ports())
    assert (outcome.status, outcome.reason) == ("paused", "max_tokens")
    assert outcome.model_calls == 2


def test_p6_a_call_without_usage_trips_the_token_budget() -> None:
    world = World([decision(act("click", target=GO.id), first=True), decision(act("wait"))])
    world.usage = None
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("paused", "max_tokens")
    assert outcome.model_calls == 1


def test_p6_with_no_price_the_cost_is_not_configured_and_never_trips() -> None:
    budgets = Budgets(
        max_actions=20, max_model_calls=10, max_tokens=10_000, max_cost_usd=0.0, max_wall_s=60.0
    )
    world = World([decision(act("click", target=GO.id), first=True)])
    outcome = run_task(task(budgets), world.ports())
    assert outcome.cost_usd is None
    assert outcome.reason != "max_cost"


def test_p6_with_a_price_the_cost_budget_is_enforced() -> None:
    price = TokenPrice(usd_per_input_token=0.01, usd_per_output_token=0.01)
    budgets = Budgets(
        max_actions=20,
        max_model_calls=10,
        max_tokens=10_000,
        max_cost_usd=1.0,
        max_wall_s=60.0,
        price=price,
    )
    world = World([decision(act("click", target=GO.id), first=True), decision(act("wait"))])
    outcome = run_task(task(budgets), world.ports())
    assert (outcome.status, outcome.reason) == ("paused", "max_cost")
    assert outcome.cost_usd == pytest.approx(1.1)


def test_combobox_legend_in_the_prompt() -> None:
    assert "ComboBox can be a text field" in SYSTEM_RULES


# ---- [V2.38] RT-03, RT-39 ----------------------------------------------------------------------

COMPOSE = element(90, "Group", "", (9, 90), 1)
COMPOSER = element(91, "Edit", "Message", (9, 91), 90)
ICON = element(93, "Button", "", (9, 93), 90)
WRITING = [*PAGE, COMPOSE, COMPOSER, ICON]


def _rerendered(name: str = "Message") -> list[Element]:
    """The composer re-rendered: a new RuntimeId, the same ledger identity (unless renamed)."""
    again = dataclasses.replace(COMPOSER, runtime_id=(9, 191), name=name)
    return [*PAGE, COMPOSE, again, ICON]


def _composer_world(later: list[Element], values: dict[tuple[int, ...], ValueFact]) -> World:
    world = World(
        [
            decision(act("type", target=COMPOSER.id, param="text"), first=True),
            decision(act("click", target=ICON.id)),
        ],
        effects=[report("own_value")],
        screen=observed(WRITING, focus=COMPOSER.runtime_id),
        later=observed(later, focus=COMPOSER.runtime_id),
    )
    world.values.update(values)
    return world


def test_rt03_a_re_rendered_composer_keeps_its_pending_text_by_identity() -> None:
    """The submit rule follows the field to its new RuntimeId: the send icon stays R2."""
    world = _composer_world(_rerendered(), {(9, 191): TYPED_VALUE})
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type"]
    assert (outcome.status, outcome.reason) == ("blocked", "commit_not_in_step")


def test_rt03_a_fallback_match_with_another_digest_asks_text_changed() -> None:
    other = ValueFact(filled=True, digest="other")
    world = _composer_world(_rerendered(), {(9, 191): other})
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("blocked", "commit_not_in_step")


def test_rt03_two_identity_matches_keep_it_pending_and_unread() -> None:
    twin = dataclasses.replace(COMPOSER, id=92, runtime_id=(9, 192))
    world = _composer_world([*_rerendered(), twin], {})
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("blocked", "commit_not_in_step")


def test_rt03_a_replaced_top_document_ends_the_pending_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "auto_proof", lambda *_a, **_k: None)  # not proven; not under test
    new_doc = dataclasses.replace(PAGE[0], runtime_id=(9, 501))
    replaced = [new_doc, *[e for e in _rerendered() if e.id != PAGE[0].id]]
    world = _composer_world(replaced, {(9, 191): TYPED_VALUE})
    run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type", "click"]


def test_rt39_a_subgoal_with_no_action_yet_is_judged_from_the_first_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []

    def spy(
        claim: object, subgoal: object, baseline: object, now: object, **_: object
    ) -> tuple[str, ...]:
        seen.append(baseline)
        return ("value_mismatch",)

    monkeypatch.setattr(runner, "check_claim", spy)
    claim = Claim(subgoal="s1", kind="value", evidence=[FIELD.id], param="text")
    first = observed(QUERY_PAGE)
    fill = [SubgoalDoc(id="s1", desc="fill", params={"text": "red shoes"}, closes="value")]
    opening = Decision(subgoals=fill, actions=[act("wait")])  # [V2.39]: a value subgoal
    world = World([opening, decision(claims=[claim])], screen=first)
    world.later = observed([*QUERY_PAGE, PAGE[3]])
    run_task(task(), world.ports())
    assert seen
    assert seen[0] is first


# ---- RT-38, RT-43: the decision's shape ------------------------------------------------------


def test_rt38_free_text_with_a_control_character_is_refused() -> None:
    from steerai.loop2.decision import validate  # noqa: PLC0415 - the validator under test

    bad = decision(act("type", target=FIELD.id, text="red" + chr(10) + "shoes"), first=True)
    assert "text_bad" in validate(
        bad, listed_ids=frozenset({FIELD.id}), goal="g red shoes", first_turn=True
    )


def test_rt43_an_ask_with_a_stop_is_a_shape_refusal() -> None:
    from steerai.loop2.decision import validate  # noqa: PLC0415 - the validator under test

    both = Decision(ask="may I?", stop="cannot")
    assert "ask_with_stop" in validate(both, listed_ids=frozenset(), goal="g", first_turn=False)


# ---- principal re-review of #321 (2f85dca) -----------------------------------------------------


def test_rr1_an_unreadable_top_document_does_not_end_the_pending_text() -> None:
    """A second root Document makes the scope unknown; that is no proof the page was replaced."""
    second_root = element(300, "Document", "Other", (9, 300), None)
    world = _composer_world([*WRITING, second_root], {})
    world.values[COMPOSER.runtime_id] = TYPED_VALUE
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type"]
    assert (outcome.status, outcome.reason) == ("blocked", "commit_not_in_step")


def test_rr2_a_growing_read_without_the_field_is_no_proof_it_is_gone() -> None:
    """The composer leaves a stable but still-growing read, then comes back re-rendered holding
    the text: the submit rule still holds."""
    tab = PAGE[6]
    without = [e for e in WRITING if e.id != COMPOSER.id]
    partial = dataclasses.replace(observed(without, focus=None), growing=True)
    back = observed(_rerendered(), focus=None)
    world = World(
        [
            decision(act("type", target=COMPOSER.id, param="text"), first=True),
            decision(act("click", target=tab.id)),
            decision(act("click", target=ICON.id)),
        ],
        effects=[report("own_value"), report("content_changed")],
        screen=observed(WRITING, focus=COMPOSER.runtime_id),
        timeline=[(1, partial), (2, back)],
    )
    world.values[(9, 191)] = TYPED_VALUE
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type", "click"]
    assert (outcome.status, outcome.reason) == ("blocked", "commit_not_in_step")


def test_rr3_history_holds_no_listing_ordinal() -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True), decision(act("click", target=99))],
        effects=[report("content_changed")],
    )
    run_task(task(), world.ports())
    for token in world.views[-1].history:
        assert not any(part.isdigit() for part in token.split(":")), token


def test_an_act_that_raises_keeps_the_action_that_may_have_landed() -> None:
    world = World([decision(act("click", target=GO.id), first=True)])
    ports = world.ports()

    def broken(_sent: object) -> ActResult:
        raise SteerError(ErrorCode.PERCEPTION_TIMEOUT, "act timed out")

    outcome = run_task(task(), dataclasses.replace(ports, act=broken))
    assert outcome.status == "stopped"
    assert len(outcome.dispatched) == 1


def test_a_document_first_seen_long_ago_is_no_clock_domain_error() -> None:
    world = World([decision(act("click", target=GO.id), first=True)])
    world.now = 10_000.0
    world.screen = dataclasses.replace(
        observed(),
        snapshot=dataclasses.replace(snapshot(PAGE), taken_at=10_000.0),
        document_since=1.0,
    )
    outcome = run_task(task(), world.ports())
    assert outcome.reason != "clock_domain"


# ---- tester's re-check of #321: surviving mutants N1, N2, N3, N5b, N7b ------------------------


def test_n1_every_typed_field_stays_pending_not_only_the_latest() -> None:
    """Type the composer, then a field in its own container; a send click beside the composer
    still submits. The landmark map is read, so the icon is provably not beside the other field:
    only the composer's pending text can block it."""
    notes = element(95, "Group", "", (9, 95), 1)
    other = element(96, "Edit", "Notes", (9, 96), 95)
    shown = [*WRITING, notes, other]
    marked = dataclasses.replace(
        observed(shown, focus=COMPOSER.runtime_id),
        landmarks={e.runtime_id: None for e in shown},
    )
    world = World(
        [
            decision(act("type", target=COMPOSER.id, param="text"), first=True),
            decision(act("type", target=other.id, param="text")),
            decision(act("click", target=ICON.id)),
        ],
        effects=[report("own_value"), report("own_value")],
        screen=marked,
    )
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type", "type"]
    assert (outcome.status, outcome.reason) == ("blocked", "commit_not_in_step")


@pytest.mark.parametrize("partial", ["unstable", "growing", "young", "poll_tier"])
def test_n2_a_read_that_may_be_partial_never_proves_the_field_gone(partial: str) -> None:
    tab = PAGE[6]
    without = [e for e in WRITING if e.id != COMPOSER.id]
    seen = observed(without, focus=None)
    if partial == "unstable":
        seen = dataclasses.replace(seen, snapshot=snapshot(without, stable=False))
    elif partial == "poll_tier":  # B2c: a poll read is the snapshot alone, never complete
        seen = poll_tier(snapshot(without))
    else:
        flags = {"growing": partial == "growing", "young": partial == "young"}
        seen = dataclasses.replace(seen, growing=flags["growing"], young=flags["young"])
    world = World(
        [
            decision(act("type", target=COMPOSER.id, param="text"), first=True),
            decision(act("click", target=tab.id)),
            decision(act("click", target=ICON.id)),
        ],
        effects=[report("own_value"), report("content_changed")],
        screen=observed(WRITING, focus=COMPOSER.runtime_id),
        timeline=[(1, seen), (2, observed(_rerendered(), focus=None))],
    )
    world.values[(9, 191)] = TYPED_VALUE
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type", "click"]
    assert (outcome.status, outcome.reason) == ("blocked", "commit_not_in_step")


def _searchbox_rerendered(value: ValueFact) -> World:
    """A searchbox typed by param, re-rendered (new RuntimeId, same identity) and read again:
    with the same digest a click beside it is a query (R1); with another it is a commit."""
    again = dataclasses.replace(FIELD, runtime_id=(9, 202))
    page = [again if e.id == FIELD.id else e for e in QUERY_PAGE]
    later = dataclasses.replace(
        observed(page, focus=None), aria_roles={again.runtime_id: "searchbox"}
    )
    world = World(
        [decision(TYPE, first=True), decision(act("click", target=GO.id))],
        effects=[report("own_value")],
        screen=observed(QUERY_PAGE, focus=FIELD.runtime_id),
        later=later,
    )
    world.values[again.runtime_id] = value
    return world


def test_n3_a_fallback_match_with_the_same_digest_keeps_the_query() -> None:
    world = _searchbox_rerendered(TYPED_VALUE)
    run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type", "click"]


def test_n3_a_fallback_match_with_another_digest_is_a_commit() -> None:
    world = _searchbox_rerendered(ValueFact(filled=True, digest="other"))
    outcome = run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type"]
    assert (outcome.status, outcome.reason) == ("blocked", "commit_not_in_step")


@pytest.mark.parametrize(
    "usage",
    [TokenUsage(-1, 5), TokenUsage(True, 5), TokenUsage(1.5, 5)],  # type: ignore[arg-type]  # invalid on purpose
    ids=["negative", "bool", "not_int"],
)
def test_n5b_invalid_usage_trips_the_token_budget(usage: TokenUsage) -> None:
    world = World([decision(act("click", target=GO.id), first=True), decision(act("wait"))])
    world.usage = usage
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("paused", "max_tokens")
    assert outcome.model_calls == 1


@pytest.mark.parametrize("since", [10_000.0 + 2 * 3600.0, float("nan"), float("inf")])
def test_n7b_a_document_time_far_in_the_future_or_broken_stops_on_clock_domain(
    since: float,
) -> None:
    world = World([decision(act("click", target=GO.id), first=True)])
    world.now = 10_000.0
    world.screen = dataclasses.replace(
        observed(),
        snapshot=dataclasses.replace(snapshot(PAGE), taken_at=10_000.0),
        document_since=since,
    )
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "clock_domain")
    assert world.dispatched == []
