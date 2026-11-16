"""[V2.68] One, [V2.69] Two and Four, intake packet rev 5 §4 and §7: the intake sufficiency check.

A first Decision whose declared subgoals are all value or state (no page subgoal, no ask, no stop)
is checked once, on the request and the declared subgoals alone (no listing), before anything
freezes. Only (sufficient, facts_finish_request) passes. Otherwise the Decision is refused through
the runner's refused-turn path with `intake_insufficient`: one strike, nothing frozen, nothing
dispatched, no claim processed. A per-task memo answers a set already judged with no new call. A
budget spent before the call pauses with its token; a failed call made counts in `intake_failed`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
from collections.abc import Mapping
from typing import Any, Literal

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.loop2 import controller
from steerai.loop2.controller import (
    INTAKE_PROMPT_VERSION,
    REPAIR_LEGEND,
    SYSTEM_RULES,
    ControllerError,
    DecideRecord,
    build_audit_prompt,
    build_intake_prompt,
)
from steerai.loop2.decide_port import DecideCalls, Reply, TaskGenerations
from steerai.loop2.decision import Claim, Decision, SubgoalDoc
from steerai.loop2.loop import Audited, IntakeView, PortInterrupted, TokenUsage
from steerai.loop2.prompt_inputs import make_intake
from steerai.loop2.runner import numbered_params, run_task
from tests.loop2.test_live_world import _world
from tests.loop2.world_fakes import (
    FIELD,
    GO,
    World,
    act,
    element,
    observed,
    report,
    task,
)
from tests.loop2.world_fakes import PAGE as SCREEN

NEWLINE = chr(10)
INTAKE_FIXED = "979763949ff64f8048a89f2820706164af5a286b7b96271c550f580bd092e109"
"""V4: sha256 of the intake's rules and schema blocks, joined by newlines."""
AUDIT_FIXED = "01ecdb87fd3fd46261f36161d45a342fc04fb0f4aaa9fa3eff2f28e4c17c48fe"
"""V4: sha256 of the audit's rules and schema blocks, joined by newlines."""

VALUE = SubgoalDoc(id="s1", desc="x", params={"text": "red shoes"}, closes="value")
STATE = SubgoalDoc(id="s2", desc="x", params={"what": "shoes"}, closes="state")
PAGE = SubgoalDoc(id="s3", desc="x", params={"text": "red shoes"}, closes="page")


def _pass() -> Audited:
    return Audited(sufficient=True, reason="facts_finish_request", usage=TokenUsage(30, 3))


def _reject() -> Audited:
    return Audited(sufficient=False, reason="request_asks_more", usage=TokenUsage(30, 3))


def _first(subs: list[SubgoalDoc], **kw: Any) -> Decision:  # Any: Decision fields
    return Decision(subgoals=subs, actions=kw.pop("actions", [act("click", target=GO.id)]), **kw)


def _world_with(decisions: list[Decision], reply: Any = None) -> World:  # Any: a reply or error
    world = World(decisions, effects=[report("content_changed")] * 4)
    world.intake_reply = _pass() if reply is None else reply
    return world


# ---- the trigger -------------------------------------------------------------------------------


@pytest.mark.parametrize("subs", [[VALUE], [STATE], [VALUE, STATE]], ids=["value", "state", "both"])
def test_a_first_decision_of_value_or_state_subgoals_is_checked(subs: list[SubgoalDoc]) -> None:
    world = _world_with([_first(subs), Decision(stop="blocked")])
    run_task(task(), world.ports())
    assert len(world.intakes) == 1


@pytest.mark.parametrize(
    "decision",
    [
        _first([VALUE, PAGE]),
        _first([PAGE]),
        Decision(subgoals=[VALUE], ask="which field?"),
        Decision(subgoals=[VALUE], stop="blocked"),
    ],
    ids=["mixed", "page", "ask", "stop"],
)
def test_other_first_decisions_are_not_checked(decision: Decision) -> None:
    world = _world_with([decision, Decision(stop="blocked")])
    run_task(task(), world.ports())
    assert world.intakes == []


def test_a_turn_after_the_freeze_is_not_checked() -> None:
    world = _world_with(
        [_first([VALUE]), Decision(actions=[act("wait")]), Decision(stop="blocked")]
    )
    run_task(task(), world.ports())
    assert len(world.intakes) == 1


# ---- the reject path, R1-R5 -----------------------------------------------------------------


def _rejected_once() -> World:
    claim = Claim(subgoal="s1", kind="value", evidence=[FIELD.id], param="text")
    return _world_with(
        [
            _first([VALUE], actions=[act("click", target=GO.id)], claims=[claim]),
            Decision(stop="blocked"),
        ],
        reply=_reject(),
    )


def test_a_rejected_set_is_a_refused_intake_turn() -> None:
    world = _rejected_once()
    outcome = run_task(task(), world.ports())
    assert "refused:intake_insufficient" in world.views[1].history  # R1
    assert ("refused",) in outcome.strikes  # R2
    assert world.views[1].first_turn  # R3: nothing frozen
    assert world.dispatched == []  # R4: nothing sent
    assert not any(line.startswith("claim:") for line in world.views[1].history)  # R5


def test_identical_rejects_end_through_the_strike_limit() -> None:
    world = _world_with([_first([VALUE])] * 5, reply=_reject())
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("paused", "no_progress")
    assert len(world.intakes) == 1  # the memo answered the repeats
    assert outcome.intake_memo_refused == 2


def test_a_passed_set_redeclared_after_a_stale_read_makes_no_second_call() -> None:
    """Rev 5 item 1: freezing waits for the stale check; the memo keeps the passed verdict."""
    from tests.loop2.test_auto_proof_every_observation import RESULTS, START  # noqa: PLC0415

    world = _world_with([_first([VALUE]), _first([VALUE]), Decision(stop="blocked")])
    world.screen = START
    world.observations = [START, RESULTS]  # the first decision's re-read is stale
    outcome = run_task(task(), world.ports())
    assert len(world.intakes) == 1
    assert outcome.intake_checks == 1
    assert world.views[1].first_turn  # the stale turn froze nothing


# ---- failures, F1-F4 ------------------------------------------------------------------------


def test_a_spent_budget_before_the_check_pauses_with_no_call() -> None:
    world = _world_with([_first([VALUE])])
    tight = task(dataclasses.replace(task().budgets, max_model_calls=1))  # the decide spends it
    outcome = run_task(tight, world.ports())
    assert world.intakes == []
    assert (outcome.status, outcome.reason) == ("paused", "max_model_calls")
    assert outcome.intake_budget_stops == 1


def test_a_prompt_over_its_bound_makes_no_call() -> None:
    world = _world_with([_first([VALUE])], reply=ControllerError("prompt_too_large"))
    outcome = run_task(task(), world.ports())
    assert (outcome.intake_no_call, outcome.intake_checks) == (1, 0)
    assert (outcome.status, outcome.reason) == ("stopped", "port_prompt_too_large")  # #437's path


def test_a_port_error_takes_decides_path_and_counts_the_call() -> None:
    world = _world_with([_first([VALUE])], reply=SteerError(ErrorCode.MODEL_ERROR, "http 529"))
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "port_model_error")
    assert (outcome.intake_failed, outcome.intake_checks) == (1, 1)


def test_a_timed_out_check_takes_decides_path_and_counts_the_call() -> None:
    """F3, tester gap on #457: a timeout is a port error like any other."""
    reply = SteerError(ErrorCode.MODEL_TIMEOUT, "deadline")
    outcome = run_task(task(), _world_with([_first([VALUE])], reply=reply).ports())
    assert (outcome.status, outcome.reason) == ("stopped", "port_model_timeout")
    assert (outcome.intake_failed, outcome.intake_checks) == (1, 1)


def test_a_kill_during_the_check_is_the_kills_stop_and_a_failed_call() -> None:
    """F3, tester gap on #457: an abandoned wait carries no authority; the latched switch stops
    the task, and the call made is counted failed."""
    world = _world_with([_first([VALUE])], reply=PortInterrupted("killed"))
    world.interrupt_after_intake = True
    world.interruption = "killed"
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "killed")
    assert (outcome.intake_failed, outcome.intake_checks) == (1, 1)
    assert world.dispatched == []


def test_a_kill_after_the_reply_keeps_the_counts_reconciled() -> None:
    """C5, #457 principal 1: the verdict is counted and memoised before the switch is read."""
    world = _world_with([_first([VALUE])])
    world.interrupt_after_intake = True
    world.interruption = "killed"
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "killed")
    assert outcome.intake_checks == outcome.intake_passed + outcome.intake_rejected == 1
    assert world.dispatched == []


def test_a_wait_given_up_before_the_send_counts_nothing() -> None:
    """F3, #457 principal 2: the port gave up before its request went out: no call."""
    world = _world_with([_first([VALUE])], reply=PortInterrupted("killed", sent=False))
    world.interrupt_after_intake = True
    world.interruption = "killed"
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "killed")
    assert (outcome.intake_checks, outcome.intake_failed) == (0, 0)
    assert outcome.model_calls == 1  # the decide alone


def test_a_switch_latched_before_the_check_sends_nothing() -> None:
    """F3, #457 principal 2: a kill read before the call: the check is never asked."""
    world = _world_with([_first([VALUE])])
    world.interrupt_in_decide = 1
    world.interruption = "killed"
    outcome = run_task(task(), world.ports())
    assert outcome.reason == "killed"
    assert world.intakes == []
    assert outcome.intake_checks == 0


def test_a_provider_refusal_through_the_real_port_ends_blocked_intake_failed() -> None:
    """F4, #457 principal 3: a refusal is a call made with no usable answer, never a port error
    (`stopped/port_model_error`)."""

    def provider(_system: list[str], _blocks: list[str]) -> Reply:
        return Reply("", "refusal", TokenUsage(7, 0))

    calls = DecideCalls(provider, interrupted=lambda: None, generations=TaskGenerations(),
                        late_error=lambda _t: None)  # fmt: skip
    intake = make_intake(calls, goal=task().goal, task_fence="taskfence01")
    world = _world_with([_first([VALUE])])
    outcome = run_task(task(), dataclasses.replace(world.ports(), intake=intake))
    assert (outcome.status, outcome.reason) == ("blocked", "intake_failed")
    assert (outcome.intake_failed, outcome.intake_checks, outcome.intake_tokens) == (1, 1, 7)


def test_a_refused_intake_turn_is_traced() -> None:
    """[V2.67] D, #457 principal 5: one record per turn, the refused intake turn included."""
    world = _rejected_once()
    records: list[dict[str, Any]] = []  # Any: JSON, read back

    def sink(record: Mapping[str, object]) -> None:
        records.append(dict(record))

    run_task(task(), dataclasses.replace(world.ports(), turn_trace=sink))
    assert [r["turn"] for r in records] == [1, 2]
    assert [f["refusal"] for f in records[0]["refusals"]] == ["intake_insufficient"]
    assert records[0]["decision"] is not None  # #457 review: the judged Decision, not "no call"


@pytest.mark.parametrize(
    "error",
    [ControllerError("not_json"), ControllerError("schema"), ControllerError("model_refusal")],
    ids=["not_json", "schema", "model_refusal"],
)
def test_an_unusable_reply_ends_blocked_intake_failed(error: ControllerError) -> None:
    error.record = DecideRecord(INTAKE_PROMPT_VERSION, 1, False, True)
    outcome = run_task(task(), _world_with([_first([VALUE])], reply=error).ports())
    assert (outcome.status, outcome.reason) == ("blocked", "intake_failed")
    assert outcome.intake_failed == 1


# ---- counting, C1-C5 ------------------------------------------------------------------------


def test_the_check_counts_one_call_and_reconciles() -> None:
    world = _world_with([_first([VALUE]), Decision(stop="blocked")])
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == 3  # two decides and the check
    assert outcome.intake_checks == outcome.intake_passed + outcome.intake_rejected
    assert outcome.intake_failed == 0
    assert outcome.intake_tokens == 33


# ---- no page text, P1-P6 --------------------------------------------------------------------


def test_the_prompt_takes_no_observation() -> None:
    assert "observed" not in inspect.signature(build_intake_prompt).parameters
    # P1/P2 sentinel, tester gap on #457: the port's view holds the subgoal lines and nothing else
    assert [f.name for f in dataclasses.fields(IntakeView)] == ["subgoals"]


def test_the_prompt_holds_positions_spans_and_no_model_text() -> None:
    view = IntakeView(subgoals=((1, "value", ((1, "red shoes"),)),))
    system, user = build_intake_prompt(
        "type red shoes in the field", view.subgoals, task_fence="taskfence01"
    )
    text = "\n".join([*system, *user])
    assert "subgoal 1 closes=value" in text
    assert "param:1" in text
    lines = text.split("\n")
    assert not any(line.startswith("request:") for line in lines)
    assert lines.count("<<<request taskfence01") == 1


def test_the_runner_hands_the_check_positions_never_model_ids() -> None:
    leak = SubgoalDoc(id="zqleakid", desc="zqdesc", params={"zqparam": "red shoes"},
                      closes="value")  # fmt: skip
    world = _world_with([_first([leak]), Decision(stop="blocked")])
    run_task(task(), world.ports())
    (view,) = world.intakes
    assert view.subgoals == ((1, "value", ((1, "red shoes"),)),)
    system, user = build_intake_prompt(task().goal, view.subgoals, task_fence="taskfence01")
    text = NEWLINE.join([*system, *user])
    for word in ("zqleakid", "zqdesc", "zqparam"):  # #457 principal: the whole prompt
        assert word not in text


# ---- versions, legend --------------------------------------------------------------------------


def test_the_intake_prompt_version_and_header() -> None:
    assert INTAKE_PROMPT_VERSION == "intake-2"
    live, _ = _world()
    assert live.header(task())["intake_prompt_version"] == "intake-2"


def test_both_judges_share_the_criterion() -> None:
    assert controller.SUFFICIENCY_CRITERION in controller.INTAKE_RULES
    assert controller.SUFFICIENCY_CRITERION in controller.AUDIT_RULES


def _fixed(system: list[str]) -> str:
    return hashlib.sha256(NEWLINE.join(system).encode()).hexdigest()


def test_each_judges_fixed_blocks_are_pinned_with_its_version() -> None:
    """V4, #457 principal 4: a change to a judge's rules or schema (the shared sentence among
    them) fails here until its version and this pin change together."""
    intake, _ = build_intake_prompt("g", (), task_fence="taskfence01")
    audit, _ = build_audit_prompt("g", (), "w", [], task_fence="taskfence01",
                                  screen_fence="scrfence01")  # fmt: skip
    pinned = (INTAKE_PROMPT_VERSION, _fixed(intake), controller.AUDIT_PROMPT_VERSION, _fixed(audit))
    assert pinned == ("intake-2", INTAKE_FIXED, "audit-4", AUDIT_FIXED)


def test_intake_insufficient_is_told_to_the_model() -> None:
    assert "intake_insufficient" in SYSTEM_RULES
    assert "intake_insufficient" in REPAIR_LEGEND


def _both_numbered(world: World) -> tuple[list[str], list[str]]:
    """One done run's intake and audit prompts, each cut to its numbered lines."""
    assert run_task(task(), world.ports()).status == "done"
    (intake,) = world.intakes
    (audit,) = world.audits
    goal, fence = task().goal, "taskfence01"
    _, intake_user = build_intake_prompt(goal, intake.subgoals, task_fence=fence)
    _, audit_user = build_audit_prompt(
        goal, audit.proofs, "w", [], task_fence=fence, screen_fence="scrfence01"
    )

    def numbered(block: str) -> list[str]:
        rows = block.split(NEWLINE)
        return [r.split(" proven=")[0] for r in rows if r.startswith(("subgoal ", "  param:"))]

    return numbered(intake_user[0]), numbered(audit_user[0])


def test_the_intake_and_the_audit_number_subgoals_and_params_alike() -> None:
    """Coordinator ruling on #455: both judges read 1-based positions in `decision_shape`'s
    order (0-based only in the trace), so one run's subgoal and param carry the same numbers in
    the intake prompt at the start and in the audit prompt at the end."""
    subs = [SubgoalDoc(id="s1", desc="x", params={"text": "red shoes"}, closes="value")]
    claim = Claim(subgoal="s1", kind="value", evidence=[FIELD.id], param="text")
    world = World(
        [
            Decision(
                subgoals=subs, actions=[act("type", target=FIELD.id, param="text", subgoal="s1")]
            ),
            Decision(claims=[claim]),
        ],
        effects=[report("own_value")],
    )
    world.intake_reply = _pass()
    intake, audit = _both_numbered(world)
    assert audit == intake
    assert intake == ["subgoal 1 closes=value", "  param:1 red shoes"]


def test_two_subgoals_with_unsorted_params_number_alike_in_both_judges() -> None:
    """#457 review 3: two subgoals, the second's param names declared out of alphabetical order;
    both judges number them by the names' sorted order (`aa` before `zz`), never as declared."""
    on: Literal["on"] = "on"
    red = element(42, "CheckBox", "Red", (9, 42), 1)
    shoes = element(43, "CheckBox", "Shoes", (9, 43), 1)
    subs = [
        SubgoalDoc(id="s1", desc="x", params={"text": "red shoes"}, closes="value"),
        SubgoalDoc(id="s2", desc="x", params={"zz": "red", "aa": "shoes"}, closes="state"),
    ]
    world = World(
        [
            Decision(
                subgoals=subs,
                actions=[act("type", target=FIELD.id, param="text", subgoal="s1")],
                claims=[
                    Claim(subgoal="s2", kind="state", evidence=[red.id], param="zz", wanted=on)
                ],
            ),
            Decision(
                claims=[
                    Claim(subgoal="s1", kind="value", evidence=[FIELD.id], param="text"),
                    Claim(subgoal="s2", kind="state", evidence=[shoes.id], param="aa", wanted=on),
                ]
            ),
        ],
        effects=[report("own_value")],
        screen=observed([*SCREEN, red, shoes], focus=FIELD.runtime_id),
    )
    world.states[(red.runtime_id, on)] = True
    world.states[(shoes.runtime_id, on)] = True
    world.intake_reply = _pass()
    intake, audit = _both_numbered(world)
    assert audit == intake
    assert intake == [
        "subgoal 1 closes=value",
        "  param:1 red shoes",
        "subgoal 2 closes=state",
        "  param:1 shoes",
        "  param:2 red",
    ]


def test_params_are_numbered_by_their_names_sorted_order() -> None:
    assert numbered_params({"zz": "red", "aa": "shoes"}) == ((1, "shoes"), (2, "red"))
    assert numbered_params({}) == ()


def test_the_intake_user_block_renders_exactly() -> None:
    """#457 review: one rendered user block, pinned whole, so a format change is seen."""
    lines = ((1, "value", ((1, "red shoes"),)), (2, "state", ((1, "shoes"), (2, "red"))))
    _, user = build_intake_prompt("search red shoes", lines, task_fence="taskfence01")
    assert user == [
        NEWLINE.join(
            [
                "<<<request taskfence01",
                "search red shoes",
                "taskfence01>>>",
                "subgoals:",
                "<<<subgoals taskfence01",
                "subgoal 1 closes=value",
                "  param:1 red shoes",
                "subgoal 2 closes=state",
                "  param:1 shoes",
                "  param:2 red",
                "taskfence01>>>",
            ]
        )
    ]
