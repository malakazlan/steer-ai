"""[V2.64] One, packet rev 4.5: the final audit is mandatory when every proof in the task is
self-written (a value or state proof on an element Steer itself acted on). It sees the request,
the proof tokens and the final listing, can only veto, and costs one call only on such tasks.
Every failure is a veto (`audit_failed`); a veto ends `blocked` / `audit_veto`."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.loop2.controller import AUDIT_PROMPT_VERSION, ControllerError, build_audit_prompt
from steerai.loop2.decision import Claim, Decision, SubgoalDoc
from steerai.loop2.loop import Audited, AuditView, PortInterrupted, TokenUsage
from steerai.loop2.runner import run_task
from tests.loop2.world_fakes import (
    FIELD,
    GO,
    PAGE,
    TYPED_VALUE,
    World,
    act,
    element,
    observed,
    report,
    task,
)

ALLOW = Audited(veto=False, reason="outcome_shown", usage=TokenUsage(50, 5))
VETO = Audited(veto=True, reason="outcome_not_shown", usage=TokenUsage(50, 5))


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
    assert outcome.audit == ("allowed", "outcome_shown")


@pytest.mark.parametrize(
    "failure",
    [SteerError(ErrorCode.MODEL_ERROR, "http 529"), ControllerError("not_json")],
    ids=["call_failed", "reply_unparsed"],
)
def test_a_failed_audit_is_a_veto(failure: Exception) -> None:
    outcome = run_task(task(), _value_only(failure).ports())
    assert (outcome.status, outcome.reason) == ("blocked", "audit_failed")


def test_with_no_model_call_left_the_audit_is_not_made_and_vetoes() -> None:
    world = _value_only(ALLOW)
    tight = task(dataclasses.replace(task().budgets, max_model_calls=2))  # two decides spend it
    outcome = run_task(tight, world.ports())
    assert (outcome.status, outcome.reason) == ("blocked", "audit_failed")
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
    assert view.proofs == (("s1", "value", "claim", "value", True),)
    assert FIELD.runtime_id in view.keep
    assert view.observed.snapshot.elements


# ---- the audit's own prompt -------------------------------------------------------------------


def test_the_audit_prompt_holds_no_desc_no_param_words_and_nothing_outside_the_fences() -> None:
    from steerai.loop2.controller import ListingRow  # noqa: PLC0415

    rows = [ListingRow(0, '1 Document "Catalog"', 1, "root")]
    system, user = build_audit_prompt(
        "search the catalog for red shoes",
        [("s1", "value", "claim", "value", True)],
        "Catalog", rows, task_fence="taskfence01", screen_fence="f" * 32,
    )  # fmt: skip
    text = "\n".join(user)
    assert "s1 closes=value proven=claim:value value_or_state" in text
    assert text.count("red shoes") == 1  # the request only, inside the task fence
    assert "taskfence01" in text
    assert "f" * 32 in text
    assert AUDIT_PROMPT_VERSION == "audit-1"
    schema = json.loads(system[1])
    assert set(schema["properties"]) == {"veto", "reason"}


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
        return Reply('{"veto": false, "reason": "outcome_shown"}', "end_turn", TokenUsage(5, 1))

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
        proofs=(("s1", "value", "claim", "value", True),),
        keep=frozenset({FIELD.runtime_id}),
        words=frozenset({"red", "shoes"}),
    )
    assert audit(view).veto is False
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
        return Reply('{"veto": false, "reason": "outcome_shown"}', "end_turn", TokenUsage(5, 1))

    calls = DecideCalls(provider, interrupted=lambda: None, generations=TaskGenerations(),
                        late_error=lambda _t: None)  # fmt: skip
    audit = make_audit(calls, goal="type red shoes", task_fence="taskfence01")
    seen = observed(PAGE)
    view = AuditView(observed=seen, handles={e.id: e.id for e in PAGE},
                     proofs=(("s1", "value", "claim", "value", True),))  # fmt: skip
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


def test_a_budget_spent_at_the_done_point_stops_the_audit_before_its_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budgets the decide loop checks (here the wall) are checked, with the audit's own call
    counted, before it is made; spent, the audit is not called and the task is `audit_failed`."""
    from steerai.loop2.guards import budget_hit as real  # noqa: PLC0415

    seen: list[int] = []

    def hit(budgets: Any, used: Any) -> Any:  # Any: the guard's own types
        seen.append(used.model_calls)
        return "max_wall" if used.model_calls >= 3 else real(budgets, used)

    monkeypatch.setattr("steerai.loop2.runner.budget_hit", hit)
    world = _value_only(ALLOW)
    outcome = run_task(task(), world.ports())
    assert world.audits == []
    assert (outcome.status, outcome.reason) == ("blocked", "audit_failed")
    assert seen[-1] == world.events.count("decide") + 1  # the audit's own call is counted
