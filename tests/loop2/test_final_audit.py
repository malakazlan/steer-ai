"""[V2.64] One, packet rev 4.5, re-pinned for audit-2 ([V2.68] One, [V2.69] One and Four): the
final audit runs on every task about to end done. It sees the request, the verified proof facts
and the final listing, and answers sufficiency; only (sufficient, facts_finish_request) lets done
stand. A failure is `audit_failed`, a veto `audit_veto` (both `blocked`); a budget spent before
it is `paused` with the budget's token and no call."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.loop2.controller import (
    AUDIT_PROMPT_VERSION,
    ControllerError,
    build_audit_prompt,
    parse_audit,
)
from steerai.loop2.decision import Claim, Decision, SubgoalDoc
from steerai.loop2.loop import Audited, AuditView, PortInterrupted, ProofLine, TokenUsage
from steerai.loop2.runner import run_task
from tests.loop2.world_fakes import (
    FIELD,
    GO,
    PAGE,
    TYPED_VALUE,
    World,
    act,
    element,
    judged_by,
    observed,
    report,
    task,
)

ALLOW = Audited(sufficient=True, reason="facts_finish_request", usage=TokenUsage(50, 5))
VETO = Audited(sufficient=False, reason="request_asks_more", usage=TokenUsage(50, 5))
LINE: ProofLine = (
    1,
    "value",
    "claim",
    "value",
    ("value_read", "equals_param"),
    ((1, "red shoes"),),
)


def _value_only(audit: Any) -> World:  # Any: an Audited, an exception or a callable
    """3d-b's value-only shape, scripted: one value subgoal, a type into the field, a value claim
    on that same field. Every proof is self-written."""
    subs = [SubgoalDoc(id="s1", desc="the field holds it", params={"text": "red shoes"},
                       closes="value")]  # fmt: skip
    world = World(
        [
            Decision(subgoals=subs, actions=[act("type", target=FIELD.id, param="text")]),
            Decision(claims=[Claim(subgoal="s1", kind="value", evidence=[FIELD.id], param="text")]),
        ],
        effects=[report("own_value")],
    )
    world.audit_reply = audit
    world.intake_on = False  # the audit's own tests: no intake call in the counts
    return world


def test_a_value_only_task_the_audit_vetoes_is_never_done() -> None:
    world = _value_only(VETO)
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("blocked", "audit_veto")
    assert len(world.audits) == 1


def test_a_value_only_task_the_audit_allows_is_done_for_one_more_call() -> None:
    world = _value_only(ALLOW)
    outcome = run_task(task(), world.ports())
    assert outcome.status == "done"
    assert outcome.model_calls == world.events.count("decide") + 1
    assert outcome.audit == ("allowed", "facts_finish_request")


@pytest.mark.parametrize(
    "failure",
    [SteerError(ErrorCode.MODEL_ERROR, "http 529"), ControllerError("not_json")],
    ids=["call_failed", "reply_unparsed"],
)
def test_a_failed_audit_is_a_veto(failure: Exception) -> None:
    outcome = run_task(task(), _value_only(failure).ports())
    assert (outcome.status, outcome.reason) == ("blocked", "audit_failed")


def test_with_no_model_call_left_the_audit_is_not_made_and_the_task_pauses() -> None:
    """[V2.69] Four (was blocked/audit_failed under audit-1)."""
    world = _value_only(ALLOW)
    tight = task(dataclasses.replace(task().budgets, max_model_calls=2))  # two decides spend it
    outcome = run_task(tight, world.ports())
    assert (outcome.status, outcome.reason) == ("paused", "max_model_calls")
    assert world.audits == []


def test_a_kill_during_the_audit_is_the_kills_stop() -> None:
    world = _value_only(PortInterrupted("killed"))
    world.interrupt_after_audit = True
    world.interruption = "killed"
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"
    assert outcome.reason == "killed"


def test_a_navigated_page_proof_is_never_audited() -> None:
    subs = [SubgoalDoc(id="s1", desc="results", params={"text": "red shoes"}, closes="page")]
    world = World(
        [
            Decision(subgoals=subs, actions=[act("type", target=FIELD.id, param="text")]),
            Decision(claims=[Claim(subgoal="s1", kind="navigated", param="text")]),
        ],
        effects=[report("url_changed_in_place")],
    )
    world.audit_reply = VETO
    run_task(task(), world.ports())
    assert world.audits == []


def test_a_value_proof_on_an_element_steer_never_acted_on_is_audited_too() -> None:
    """Principal 1, rev 4.6: every task closed only by value or state claims is audited; a
    pre-filled field costs one call."""
    subs = [SubgoalDoc(id="s1", desc="it holds it", params={"text": "red shoes"}, closes="value")]
    world = World(
        [
            Decision(subgoals=subs, actions=[act("click", target=GO.id)]),
            Decision(claims=[Claim(subgoal="s1", kind="value", evidence=[FIELD.id], param="text")]),
        ]
    )
    world.values[FIELD.runtime_id] = TYPED_VALUE
    world.audit_reply = VETO
    outcome = run_task(task(), world.ports())
    assert len(world.audits) == 1
    assert outcome.status != "done"


def _combo_mirror() -> World:
    """Principal 1: an editable ComboBox exposes the Value of its child Edit. Type into the
    child, claim the value on the parent: the proof cites an element Steer never acted on."""
    combo = element(20, "ComboBox", "Search", (9, 20), 1)
    child = element(21, "Edit", "Search", (9, 21), 20)
    subs = [SubgoalDoc(id="s1", desc="it holds it", params={"text": "red shoes"}, closes="value")]
    world = World(
        [
            Decision(subgoals=subs, actions=[act("type", target=child.id, param="text")]),
            Decision(claims=[Claim(subgoal="s1", kind="value", evidence=[combo.id], param="text")]),
        ],
        effects=[report("own_value")],
        screen=observed([*PAGE, combo, child], focus=child.runtime_id),
    )
    world.values[combo.runtime_id] = TYPED_VALUE
    world.values[child.runtime_id] = TYPED_VALUE
    world.audit_reply = VETO
    return world


def test_a_value_claimed_on_a_mirror_of_the_typed_field_is_still_audited() -> None:
    world = _combo_mirror()
    outcome = run_task(task(), world.ports())
    assert len(world.audits) == 1
    assert outcome.status != "done"


def test_a_task_that_is_not_done_never_calls_the_audit() -> None:
    world = World([Decision(stop="cannot")])
    world.audit_reply = VETO
    run_task(task(), world.ports())
    assert world.audits == []


def test_the_audit_sees_the_proof_tokens_and_the_final_listing() -> None:
    world = _value_only(ALLOW)
    run_task(task(), world.ports())
    view = world.audits[0]
    assert isinstance(view, AuditView)
    assert view.proofs == (LINE,)
    assert FIELD.runtime_id in view.keep
    assert view.observed.snapshot.elements


# ---- the audit's own prompt -------------------------------------------------------------------


def test_the_audit_prompt_holds_no_desc_no_param_words_and_nothing_outside_the_fences() -> None:
    from steerai.loop2.controller import ListingRow  # noqa: PLC0415

    rows = [ListingRow(0, '1 Document "Catalog"', 1, "root")]
    system, user = build_audit_prompt(
        "search the catalog for red shoes",
        [LINE],
        "Catalog", rows, task_fence="taskfence01", screen_fence="f" * 32,
    )  # fmt: skip
    text = "\n".join(user)
    assert "subgoal 1 closes=value proven=claim:value value_read:equals_param" in text
    assert text.count("red shoes") == 2  # the request and the param's span, both fenced
    assert "taskfence01" in text
    assert "f" * 32 in text
    assert AUDIT_PROMPT_VERSION == "audit-2"
    schema = json.loads(system[1])
    assert set(schema["properties"]) == {"sufficient", "reason"}


# ---- #404 principal 2: the audit's listing is bounded as decide's ------------------------


SENTINEL_DESC = "zqsentineldesc"
OFF_LISTING = "zqofflistingname"


def test_make_audit_carries_no_desc_and_nothing_from_the_page_outside_the_listing() -> None:
    """Rev 4.5 test 9: a sentinel desc and a page name the cap leaves out never reach the
    audit's blocks; the listing is selected with decide's keep set and cap."""
    from steerai.loop2.decide_port import DecideCalls, Reply, TaskGenerations  # noqa: PLC0415
    from steerai.loop2.prompt_inputs import make_audit  # noqa: PLC0415

    sent: list[list[str]] = []

    def provider(_system: list[str], blocks: list[str]) -> Reply:
        sent.append(blocks)
        reply = '{"sufficient": true, "reason": "facts_finish_request"}'
        return Reply(reply, "end_turn", TokenUsage(5, 1))

    many = [element(100 + i, "Button", f"Item {i}", (9, 100 + i), 1) for i in range(400)]
    far = dataclasses.replace(element(999, "Button", OFF_LISTING, (9, 999), 1), offscreen=True)
    seen = observed([*PAGE, *many, far])
    handles = {e.id: e.id for e in seen.snapshot.elements}
    calls = DecideCalls(provider, interrupted=lambda: None, generations=TaskGenerations(),
                        late_error=lambda _t: None)  # fmt: skip
    audit = make_audit(calls, goal="type red shoes in the field", task_fence="taskfence01")
    view = AuditView(
        observed=seen,
        handles=handles,
        proofs=(LINE,),
        keep=frozenset({FIELD.runtime_id}),
        words=frozenset({"red", "shoes"}),
    )
    assert audit(view).sufficient is True
    text = "\n".join(sent[0])
    assert SENTINEL_DESC not in text
    assert OFF_LISTING not in text
    assert '"Search"' in text  # the kept field is listed


def test_an_audit_prompt_over_the_bound_is_failed_with_no_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Principal 2: the same byte bound as decide; over it, no call is made."""
    from steerai.loop2 import controller  # noqa: PLC0415
    from steerai.loop2.decide_port import DecideCalls, Reply, TaskGenerations  # noqa: PLC0415
    from steerai.loop2.prompt_inputs import make_audit  # noqa: PLC0415

    monkeypatch.setattr(controller, "PROMPT_TOKEN_CEILING", 10)
    called: list[int] = []

    def provider(_system: list[str], _blocks: list[str]) -> Reply:
        called.append(1)
        reply = '{"sufficient": true, "reason": "facts_finish_request"}'
        return Reply(reply, "end_turn", TokenUsage(5, 1))

    calls = DecideCalls(provider, interrupted=lambda: None, generations=TaskGenerations(),
                        late_error=lambda _t: None)  # fmt: skip
    audit = make_audit(calls, goal="type red shoes", task_fence="taskfence01")
    seen = observed(PAGE)
    view = AuditView(observed=seen, handles={e.id: e.id for e in PAGE},
                     proofs=(LINE,))  # fmt: skip
    with pytest.raises(ControllerError) as refused:
        audit(view)
    assert refused.value.code == "prompt_too_large"
    assert refused.value.record is None  # no call was made
    assert called == []


def test_an_audit_that_made_no_call_is_not_counted_as_one() -> None:
    """Non-blocking: a refusal before any call (`prompt_too_large`, a fence in the data) is
    `audit_failed` with no model call counted and no NaN tokens."""
    world = _value_only(ControllerError("prompt_too_large"))
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("blocked", "audit_failed")
    assert outcome.model_calls == world.events.count("decide")
    assert outcome.audit == ("failed", "audit_no_call")


# ---- #404 principal 3: every budget is checked before the audit's call ------------------------


DECIDES = 2
"""`_value_only` spends two decide calls before its done point."""


def _calls_budget(limit: int) -> Any:  # Any: a Task
    return task(dataclasses.replace(task().budgets, max_model_calls=limit))


def test_with_one_model_call_left_the_audit_runs_and_the_task_is_done() -> None:
    """The real guard, as decide uses it (`not spent < limit` before the call): one call left
    is enough for the audit."""
    world = _value_only(ALLOW)
    outcome = run_task(_calls_budget(DECIDES + 1), world.ports())
    assert world.events.count("decide") == DECIDES
    assert len(world.audits) == 1
    assert (outcome.status, outcome.model_calls) == ("done", DECIDES + 1)


def test_with_no_model_call_left_the_audit_makes_no_call_and_pauses() -> None:
    """[V2.69] Four (was blocked/audit_failed under audit-1): no call, the budget's token."""
    world = _value_only(ALLOW)
    outcome = run_task(_calls_budget(DECIDES), world.ports())
    assert world.audits == []
    assert (outcome.status, outcome.reason) == ("paused", "max_model_calls")
    assert outcome.model_calls == DECIDES
    assert outcome.audit_budget_stops == 1


# ---- #404 principal 4: a contradictory reply fails closed --------------------------------------


@pytest.mark.parametrize(
    "contradiction",
    [
        Audited(sufficient=True, reason="request_asks_more"),
        Audited(sufficient=True, reason="unclear"),
        Audited(sufficient=False, reason="facts_finish_request"),
    ],
    ids=["sufficient_but_asks_more", "sufficient_but_unclear", "insufficient_but_finishes"],
)
def test_only_sufficient_with_facts_finish_request_lets_done_stand(
    contradiction: Audited,
) -> None:
    outcome = run_task(task(), _value_only(contradiction).ports())
    assert (outcome.status, outcome.reason) == ("blocked", "audit_veto")


# ---- #404 principal 5: the rev 4.5 tests still missing ------------------------------------------


def test_an_auto_proof_is_audited_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """[V2.69] One (was rev 4.5 test 3, "never audited"): a page subgoal closed by the auto proof
    is audited like every task about to end done; its veto stands."""
    monkeypatch.setattr("steerai.loop2.runner.judge", judged_by(lambda *_a, **_k: "url_query"))
    subs = [SubgoalDoc(id="s1", desc="results", params={"text": "red shoes"}, closes="page")]
    world = World(
        [Decision(subgoals=subs, actions=[act("type", target=FIELD.id, param="text")])],
        effects=[report("url_changed_in_place")],
    )
    world.after = dataclasses.replace(observed(), url="https://shop.test/search?q=red+shoes")
    world.later = world.after
    world.audit_reply = VETO
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("blocked", "audit_veto")
    assert len(world.audits) == 1


def test_a_mixed_task_value_plus_navigated_is_audited_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[V2.69] One (was rev 4.5 test 4, "never audited"): one value proof and one page proof."""
    monkeypatch.setattr("steerai.loop2.runner.judge", judged_by(lambda *_a, **_k: "url_query"))
    subs = [
        SubgoalDoc(id="s1", desc="field", params={"text": "red shoes"}, closes="value"),
        SubgoalDoc(id="s2", desc="results", params={"text": "red shoes"}, closes="page"),
    ]
    claim = Claim(subgoal="s1", kind="value", evidence=[FIELD.id], param="text")
    world = World(
        [
            Decision(
                subgoals=subs, actions=[act("type", target=FIELD.id, param="text", subgoal="s2")]
            ),
            Decision(claims=[claim]),
        ],
        effects=[report("own_value", "url_changed_in_place")],
    )
    world.after = dataclasses.replace(observed(), url="https://shop.test/search?q=red+shoes")
    world.later = world.after
    world.audit_reply = VETO
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("blocked", "audit_veto")
    assert len(world.audits) == 1


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        ('{"sufficient": true, "reason": "looks_fine"}', "schema"),
        ('{"sufficient": true}', "schema"),
        ('{"sufficient": "yes", "reason": "facts_finish_request"}', "schema"),
        ('{"veto": false, "reason": "outcome_shown"}', "schema"),
        ("not json at all", "not_json"),
    ],
    ids=["unknown_reason", "no_reason", "sufficient_not_bool", "the_old_schema", "unparsed"],
)
def test_parse_audit_refuses_anything_but_its_schema(reply: str, code: str) -> None:
    """Rev 4.5 test 6, on the parser itself."""
    with pytest.raises(ControllerError) as refused:
        parse_audit(reply)
    assert refused.value.code == code


def test_the_audits_tokens_are_counted_in_used() -> None:
    """Rev 4.5 test 10."""
    world = _value_only(
        Audited(sufficient=True, reason="facts_finish_request", usage=TokenUsage(500, 7))
    )
    outcome = run_task(task(), world.ports())
    decides = world.events.count("decide")
    assert outcome.usage.input_tokens == decides * 100 + 500
    assert outcome.usage.output_tokens == decides * 10 + 7
