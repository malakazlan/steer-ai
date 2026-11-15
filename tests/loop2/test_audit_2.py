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

import pytest

from steerai.loop2 import controller
from steerai.loop2.controller import AUDIT_PROMPT_VERSION, ControllerError, DecideRecord
from steerai.loop2.decide_port import DecideCalls, Reply, TaskGenerations
from steerai.loop2.decision import Claim, Decision, SubgoalDoc
from steerai.loop2.loop import Audited, AuditView, PortInterrupted, TokenUsage
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
    assert outcome.audit_tokens == 55  # the allowed call's 50 + 5 (#452 tester B2)


def test_a_vetoed_task_counts_one_vetoed_audit_call() -> None:
    outcome = run_task(task(), _page_only(_veto()).ports())
    assert (outcome.audit_allowed, outcome.audit_vetoed, outcome.audit_failed) == (0, 1, 0)
    assert outcome.audit_calls == 1


# ---- rev 4 item 5: free text Steer typed is masked in the audit's view --------------------------

FREE = "zqfreetext"


def test_free_text_steer_typed_never_reaches_the_audit() -> None:
    """A free `type` text echoed whole by a result row and the window title is replaced by a fixed
    token before the audit sees the page (a part-echo is the eval's measured escape rate)."""
    from tests.loop2.world_fakes import element  # noqa: PLC0415 - this test only

    subs = [SubgoalDoc(id="s1", desc="results", params={"text": "red shoes"}, closes="page")]
    echo = element(40, "Text", f"Results for {FREE}", (9, 40), 1)
    after = page([RESULT, echo], title=f"{FREE} - Shop")
    world = World(
        [Decision(subgoals=subs, actions=[act("type", target=FIELD.id, text=FREE)])],
        effects=[report("own_value", "url_changed_in_place")],
        screen=dataclasses.replace(before(), focus=FIELD.runtime_id),
    )
    world.after = dataclasses.replace(after, focus=FIELD.runtime_id)
    text = _audit_prompt(world)
    assert FREE not in text.casefold()
    assert "[typed_text]" in text


# ---- #452 REVISE: the audit's budget check, the mask's folding, the failure counts ---------


def test_the_last_allowed_action_can_still_close_and_be_audited() -> None:
    """Principal 1: actions are not a reason to skip the audit; the task that used its last
    allowed action to close ends done (packet §4 lists calls, tokens, cost and wall time)."""
    world = _page_only(_allow())
    outcome = run_task(task(dataclasses.replace(task().budgets, max_actions=1)), world.ports())
    assert outcome.status == "done"
    assert len(world.audits) == 1


def test_a_spent_token_budget_before_the_audit_pauses_with_its_token() -> None:
    """Tester N3: every budget the audit checks, not only the call count."""
    world = _value_only()
    world.audit_reply = _allow()
    tight = task(dataclasses.replace(task().budgets, max_tokens=150))
    outcome = run_task(tight, world.ports())
    assert world.audits == []
    assert (outcome.status, outcome.reason) == ("paused", "max_tokens")
    assert outcome.audit_budget_stops == 1


@pytest.mark.parametrize(
    ("typed", "echo"),
    [
        ("Hauptstraße 5", "Hauptstraße 5"),
        ("Hauptstraße 5", "HAUPTSTRASSE 5"),
        ("İstanbul", "İstanbul"),
        ("red Shoes", "RED SHOES"),
        ("red  shoes", "red\u00a0shoes"),
        ("\uff5a\uff51\uff57\uff49\uff44\uff45", "zqwide"),
        ("no 5", "\u2116 5"),
        ("40 \u00b0c", "40 \u2103"),
    ],
    ids=["eszett", "eszett_upper", "dotted_i", "case", "spaces", "nfkc", "numero", "celsius"],
)
def test_a_whole_echo_is_masked_whatever_its_case_and_form(typed: str, echo: str) -> None:
    """Principal 2, tester G1/G2: both sides are folded the same way (NFKC, whitespace, full
    casefold) before matching."""
    from steerai.loop2.prompt_inputs import masked  # noqa: PLC0415
    from tests.loop2.world_fakes import element, observed  # noqa: PLC0415

    row = element(40, "Text", f"Results for {echo}", (9, 40), 1)
    seen = observed([row])
    seen = dataclasses.replace(seen, snapshot=dataclasses.replace(seen.snapshot, title=echo))
    out = masked(seen, [typed])
    names = [e.name for e in out.snapshot.elements]
    assert "results for [typed_text]" in names  # a masked name is shown folded
    assert out.snapshot.title == "[typed_text]"


def test_a_short_typed_text_masks_whole_words_only() -> None:
    """Tester N1: a typed "a" never garbles "Search" or "Shop - Cart"."""
    from steerai.loop2.prompt_inputs import masked  # noqa: PLC0415
    from tests.loop2.world_fakes import element, observed  # noqa: PLC0415

    rows = [element(40, "Button", "Search", (9, 40), 1), element(41, "Text", "a", (9, 41), 1)]
    out = masked(observed(rows), ["a"])
    names = {e.name for e in out.snapshot.elements}
    assert "Search" in names
    assert "[typed_text]" in names


def test_a_name_with_no_echo_is_left_exactly_as_it_was() -> None:
    from steerai.loop2.prompt_inputs import masked  # noqa: PLC0415
    from tests.loop2.world_fakes import element, observed  # noqa: PLC0415

    row = element(40, "Text", "Ünïcode  Name", (9, 40), 1)
    out = masked(observed([row]), ["zqabsent"])
    assert [e.name for e in out.snapshot.elements] == [
        e.name for e in observed([row]).snapshot.elements
    ]


def test_a_failed_audit_call_counts_its_call_and_tokens() -> None:
    """Tester G3: a call made that fails is a call, a failure, and its tokens."""
    failed = ControllerError("not_json")
    failed.record = DecideRecord(AUDIT_PROMPT_VERSION, 1, False, True)
    failed.usage = TokenUsage(40, 4)
    world = _value_only()
    world.audit_reply = failed
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("blocked", "audit_failed")
    assert (outcome.audit_calls, outcome.audit_failed, outcome.audit_tokens) == (1, 1, 44)
    assert (
        outcome.audit_calls == outcome.audit_allowed + outcome.audit_vetoed + outcome.audit_failed
    )


@pytest.mark.parametrize("how", ["no_port", "over_the_bound"])
def test_an_audit_that_made_no_call_is_counted_apart(how: str) -> None:
    """Tester G3, principal note: one token for every no-call audit (`audit_no_call`)."""
    world = _value_only()
    ports = world.ports()
    if how == "no_port":
        ports = dataclasses.replace(ports, audit=None)
    else:
        world.audit_reply = ControllerError("prompt_too_large")
    outcome = run_task(task(), ports)
    assert outcome.status != "done"
    assert (outcome.audit_no_call, outcome.audit_calls) == (1, 0)
    assert outcome.audit == ("failed", "audit_no_call")


def test_a_kill_during_the_audit_counts_a_failed_call() -> None:
    world = _value_only()
    world.audit_reply = PortInterrupted("killed")
    world.interrupt_after_audit = True
    world.interruption = "killed"
    outcome = run_task(task(), world.ports())
    assert outcome.reason == "killed"
    assert (outcome.audit_calls, outcome.audit_failed) == (1, 1)


def test_done_means_exactly_one_allowed_audit() -> None:
    for world in (_page_only(_allow()), _value_only()):
        outcome = run_task(task(), world.ports())
        assert outcome.status == "done"
        assert (outcome.audit_allowed, outcome.audit_calls) == (1, 1)


def test_the_stage_summary_keeps_the_audit_invariant() -> None:
    """Packet K3: the summed counts still reconcile."""
    import json  # noqa: PLC0415

    from steerai.loop2 import cli  # noqa: PLC0415
    from steerai.loop2.stage1 import accept  # noqa: PLC0415
    from tests.loop2.test_stage1 import DONE, _final, _ok  # noqa: PLC0415

    finals = []
    for world in (_page_only(_allow()), _page_only(_veto()), _value_only()):
        outcome = run_task(task(), world.ports())
        line = json.loads(json.dumps(cli._result(outcome, task().budgets, {"monitor_lost": 0})))
        finals.append(_final(counts=line["counts"]))
    runs = [_ok(DONE, i + 1, final=f) for i, f in enumerate(finals)]
    counts = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report["counts"]
    assert counts["audit_calls"] == 3
    assert counts["audit_calls"] == (
        counts["audit_allowed"] + counts["audit_vetoed"] + counts["audit_failed"]
    )
