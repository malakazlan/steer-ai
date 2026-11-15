"""[V2.68] One, [V2.69] One and Four, intake packet rev 3 §5 and §7 (A1, A4, and the audit-2 parts
of V1-V4): audit-2.

- The final audit runs on EVERY task about to end done, whatever closed its subgoals (closes
  #436).
- Its question is sufficiency over facts code verified: value proofs carry
  `value_read:equals_param`, state proofs `state_read:<wanted>`, and page proofs their proof
  token.
- It names subgoals and params by position, never by the model's ids or names, with the request
  and the params' spans inside the task fence.
- A budget hit before the audit makes no call and ends `paused` with the budget's token,
  counted, never done."""

from __future__ import annotations

import dataclasses

from steerai.loop2 import controller
from steerai.loop2.controller import AUDIT_PROMPT_VERSION
from steerai.loop2.decide_port import DecideCalls, Reply, TaskGenerations
from steerai.loop2.decision import Claim, Decision, SubgoalDoc
from steerai.loop2.loop import Audited, AuditView, TokenUsage
from steerai.loop2.prompt_inputs import make_audit
from steerai.loop2.runner import run_task
from tests.loop2.test_live_world import _world
from tests.loop2.test_navigated import before, page
from tests.loop2.world_fakes import FIELD, RESULT, World, act, report, task


def _allow() -> Audited:
    """Rev 4 item 4: both judges answer {sufficient, reason}."""
    return Audited(sufficient=True, reason="facts_finish_request", usage=TokenUsage(50, 5))


def _veto() -> Audited:
    return Audited(sufficient=False, reason="request_asks_more", usage=TokenUsage(50, 5))


LEAK_ID = "zqleakid"
LEAK_PARAM = "zqleakparam"
FENCE = "taskfence01"


def _page_only(audit: object) -> World:
    """A page subgoal the auto proof closes on the type's after-read (results and `?q=`)."""
    subs = [SubgoalDoc(id="s1", desc="results", params={"text": "red shoes"}, closes="page")]
    world = World(
        [Decision(subgoals=subs, actions=[act("type", target=FIELD.id, param="text")])],
        effects=[report("own_value", "url_changed_in_place")],
        screen=dataclasses.replace(before(), focus=FIELD.runtime_id),
    )
    world.after = dataclasses.replace(page([RESULT]), focus=FIELD.runtime_id)
    world.audit_reply = audit  # type: ignore[assignment]  # an Audited
    return world


def _value_only(*, sid: str = "s1", param: str = "text") -> World:
    subs = [SubgoalDoc(id=sid, desc="the field holds it", params={param: "red shoes"},
                       closes="value")]  # fmt: skip
    return World(
        [
            Decision(
                subgoals=subs, actions=[act("type", target=FIELD.id, param=param, subgoal=sid)]
            ),
            Decision(claims=[Claim(subgoal=sid, kind="value", evidence=[FIELD.id], param=param)]),
        ],
        effects=[report("own_value")],
    )


# ---- A1: every task about to end done is audited ----------------------------------------------


def test_a_page_only_task_closed_by_the_auto_proof_is_audited() -> None:
    world = _page_only(_allow())
    outcome = run_task(task(), world.ports())
    assert len(world.audits) == 1
    assert outcome.status == "done"


def test_a_page_only_task_the_audit_vetoes_is_never_done() -> None:
    world = _page_only(_veto())
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("blocked", "audit_veto")


# ---- A4: a budget hit before the audit: no call, paused with its token, counted --------------


def test_with_no_call_left_the_audit_is_not_made_and_the_task_pauses() -> None:
    world = _value_only()
    world.audit_reply = _allow()
    tight = task(dataclasses.replace(task().budgets, max_model_calls=2))  # two decides spend it
    outcome = run_task(tight, world.ports())
    assert world.audits == []
    assert (outcome.status, outcome.reason) == ("paused", "max_model_calls")
    assert outcome.audit_budget_stops == 1


def test_a_fully_proven_task_without_its_audit_is_never_done() -> None:
    world = _value_only()
    tight = task(dataclasses.replace(task().budgets, max_model_calls=2))
    outcome = run_task(tight, world.ports())
    assert outcome.status != "done"


# ---- the prompt: positions, fenced spans, closed read tokens, no model ids or names -----------


def _audit_prompt(world: World) -> str:
    """Run the task with the real audit prompt builder over a fake provider, and return what it
    sent."""
    sent: list[str] = []

    def provider(system: list[str], blocks: list[str]) -> Reply:
        sent.append("\n".join([*system, *blocks]))
        reply = '{"sufficient": true, "reason": "facts_finish_request"}'
        return Reply(reply, "end_turn", TokenUsage(5, 1))

    calls = DecideCalls(provider, interrupted=lambda: None, generations=TaskGenerations(),
                        late_error=lambda _t: None)  # fmt: skip
    real = make_audit(calls, goal=task().goal, task_fence=FENCE)
    seen: list[AuditView] = []

    def audit(view: AuditView) -> Audited:
        seen.append(view)
        return real(view)

    run_task(task(), dataclasses.replace(world.ports(), audit=audit))
    assert seen, "the audit ran"
    return sent[0]


def test_a_value_proof_line_says_code_read_the_requests_words() -> None:
    text = _audit_prompt(_value_only())
    assert "value_read:equals_param" in text


def test_the_audit_names_subgoals_and_params_by_position_only() -> None:
    text = _audit_prompt(_value_only(sid=LEAK_ID, param=LEAK_PARAM))
    assert LEAK_ID not in text
    assert LEAK_PARAM not in text
    assert "param:1" in text


def test_the_request_and_the_spans_are_inside_the_task_fence() -> None:
    text = _audit_prompt(_value_only())
    lines = text.split("\n")
    opened = [i for i, line in enumerate(lines) if line.startswith("<<<") and FENCE in line]
    closed = [i for i, line in enumerate(lines) if line == f"{FENCE}>>>"]
    inside = {i for o in opened for c in closed if c > o for i in range(o, c + 1)}
    words = [i for i, line in enumerate(lines) if "red shoes" in line]
    assert words
    assert all(i in inside for i in words)
    assert not any(line.startswith("request:") for line in lines)


def test_a_page_proof_line_carries_its_proof_token() -> None:
    text = _audit_prompt(_page_only(_allow()))
    assert "navigated:results_with_url" in text


# ---- V1-V4, the audit-2 parts -------------------------------------------------------------------


def test_the_audit_prompt_version_is_audit_2() -> None:
    assert AUDIT_PROMPT_VERSION == "audit-2"


def test_the_run_header_names_the_audit_prompt_version() -> None:
    world, _ = _world()
    assert world.header(task())["audit_prompt_version"] == "audit-2"


def test_the_audit_rules_carry_the_shared_sufficiency_criterion() -> None:
    assert controller.SUFFICIENCY_CRITERION in controller.AUDIT_RULES
    assert "Code has verified the facts below; treat them as true." in controller.AUDIT_RULES


# ---- rev 4 item 4: counts reconcile from the final line ------------------------------------------


def test_an_allowed_done_counts_one_allowed_audit_call() -> None:
    world = _page_only(_allow())
    outcome = run_task(task(), world.ports())
    assert outcome.status == "done"
    assert (outcome.audit_allowed, outcome.audit_vetoed, outcome.audit_failed) == (1, 0, 0)
    assert outcome.audit_calls == 1


def test_a_vetoed_task_counts_one_vetoed_audit_call() -> None:
    outcome = run_task(task(), _page_only(_veto()).ports())
    assert (outcome.audit_allowed, outcome.audit_vetoed, outcome.audit_failed) == (0, 1, 0)
    assert outcome.audit_calls == 1


# ---- rev 4 item 5: free text Steer typed is masked in the audit's view --------------------------

FREE = "zqfreetext"


def test_free_text_steer_typed_never_reaches_the_audit() -> None:
    """A free `type` text echoed by a heading and the window title is replaced by a fixed token
    before the audit sees the page."""
    from tests.loop2.world_fakes import element  # noqa: PLC0415 - this test only

    subs = [SubgoalDoc(id="s1", desc="results", params={"text": "red shoes"}, closes="page")]
    echo = element(40, "Text", f"Results for {FREE}", (9, 40), 1)
    after = page([RESULT, echo], title=f"{FREE} - Shop")
    world = World(
        [Decision(subgoals=subs, actions=[act("type", target=FIELD.id, text=f"red shoes {FREE}")])],
        effects=[report("own_value", "url_changed_in_place")],
        screen=dataclasses.replace(before(), focus=FIELD.runtime_id),
    )
    world.after = dataclasses.replace(after, focus=FIELD.runtime_id)
    text = _audit_prompt(world)
    assert FREE not in text.casefold()
    assert "[typed_text]" in text
