"""The gate ask inside the v2 loop, `bootstrap` mode (V2 §7.2, §8.3, §8.4, §8.5; packet
v2-step4-claims §3.4, §3.5; [V2.15] Q5, Q6, Q7). Red on the step 4 stub."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from steerai.goal_match import Verdict, build_prompt
from steerai.loop2.consent import (
    Consents,
    GateEnd,
    GateRequest,
    GateSteps,
    GoalRow,
    RefusalToken,
    gate_pass,
    goal_prompt,
    goal_reasons,
    goal_row,
)
from steerai.orchestrator.contracts import GateDecision, Risk
from steerai.orchestrator.gate import Ask
from steerai.orchestrator.ledger import Admission
from tests.loop2.world_fakes import GOAL, SEND, act, pending

KEY = "k-1"
REQUEST = GateRequest(
    action_key=KEY,
    verb="click",
    commit="send",
    risk="R2",
    role="Button",
    name="Send",
    text="red shoes",
    text_from="request",
    window="Shop",
)


def yes(ask: Ask) -> str:
    return f"yes:{ask.decision_id}"


@dataclass
class Script:
    """Plays the §3.4 steps that touch the world and records their order."""

    checks: RefusalToken | None = None
    admission: Admission = "fresh"
    verdict: Verdict = "serves"
    answer: Callable[[Ask], str] = yes
    after: RefusalToken | None = None
    written: bool = True
    events: list[str] = field(default_factory=list)
    asks: list[Ask] = field(default_factory=list)
    rows: list[GoalRow] = field(default_factory=list)
    dispatched: list[GateDecision] = field(default_factory=list)

    def _checks(self) -> RefusalToken | None:
        self.events.append("checks")
        return self.checks

    def _admit(self, key: str) -> Admission:  # noqa: ARG002 - the GateSteps shape; scripted
        self.events.append("admit")
        return self.admission

    def _goal(self, row: GoalRow) -> Verdict:
        self.events.append("goal_check")
        self.rows.append(row)
        return self.verdict

    def _ask(self, ask: Ask) -> str:
        self.events.append("ask")
        self.asks.append(ask)
        return self.answer(ask)

    def _after(self) -> RefusalToken | None:
        self.events.append("after_yes")
        return self.after

    def _write(self, decision: GateDecision) -> bool:  # noqa: ARG002 - the GateSteps shape
        self.events.append("write_ahead")
        return self.written

    def _dispatch(self, decision: GateDecision) -> None:
        self.events.append("dispatch")
        self.dispatched.append(decision)

    def steps(self) -> GateSteps:
        return GateSteps(
            checks=self._checks,
            admit=self._admit,
            goal_check=self._goal,
            ask=self._ask,
            after_yes=self._after,
            write_ahead=self._write,
            dispatch=self._dispatch,
        )


def run(
    script: Script, request: GateRequest = REQUEST, consents: Consents | None = None
) -> GateEnd:
    return gate_pass(request, script.steps(), consents or Consents(), task_id="t", goal=GOAL)


ORDER = ["checks", "admit", "goal_check", "ask", "after_yes", "write_ahead", "dispatch"]


@pending("§3.4: checks, ledger, goal check, ask, after-yes, write-ahead, dispatch, in order")
def test_a_yes_runs_every_step_in_order_and_dispatches_once() -> None:
    script = Script()
    end = run(script)
    assert (end.kind, script.events) == ("dispatched", ORDER)
    assert len(script.dispatched) == 1


@pending("§8.3, [V2.15] Q6: the same answer rule for R2 and R3")
@pytest.mark.parametrize("risk", ["R2", "R3"])
def test_every_r2_and_r3_action_asks_in_bootstrap_mode(risk: Risk) -> None:
    script = Script()
    run(script, dataclasses.replace(REQUEST, risk=risk))
    assert len(script.asks) == 1
    assert script.asks[0].risk == risk


@pending("§3.4 steps 3 and 4: a refused check asks nothing and dispatches nothing")
@pytest.mark.parametrize(
    "token", ["commit_undeclared", "no_strict_shape", "not_in_step", "composer_differs"]
)
def test_a_failed_check_refuses_before_the_ledger_and_the_ask(token: RefusalToken) -> None:
    script = Script(checks=token)
    end = run(script)
    assert (end.kind, end.token) == ("refused", token)
    assert script.events == ["checks"]


@pending("§8.4, §3.4 step 5: a key closed done refuses done_already with no ask")
def test_a_done_key_refuses_done_already_without_a_goal_check_or_an_ask() -> None:
    script = Script(admission="done")
    end = run(script)
    assert (end.kind, end.token) == ("refused", "done_already")
    assert script.events == ["checks", "admit"]


@pending("§8.4: an unknown outcome confirms, never re-sends")
def test_a_key_after_an_unknown_outcome_asks_with_previous_attempt() -> None:
    script = Script(admission="confirm")
    run(script)
    assert script.asks[0].previous_attempt is not None


@pending("§8.4: an unknown outcome confirms, never re-sends")
def test_a_key_after_an_unknown_outcome_never_dispatches_without_a_new_yes() -> None:
    script = Script(admission="confirm", answer=lambda _ask: "no")
    run(script)
    assert script.dispatched == []


@pending("§3.4 step 8: a no ends the task blocked, user_declined")
@pytest.mark.parametrize("answer", ["no", "no:{id}"])
def test_a_no_ends_blocked_user_declined(answer: str) -> None:
    script = Script(answer=lambda ask: answer.format(id=ask.decision_id))
    end = run(script)
    assert (end.kind, end.status, end.token) == ("ended", "blocked", "user_declined")
    assert "write_ahead" not in script.events
    assert script.dispatched == []


@pending("§3.4 step 8, [V2.15] Q7: no answer ends the task paused, unanswered")
def test_no_answer_ends_paused_unanswered() -> None:
    script = Script(answer=lambda _ask: "unanswered")
    end = run(script)
    assert (end.kind, end.status, end.token) == ("ended", "paused", "unanswered")
    assert script.dispatched == []


@pending("§3.4 step 8: the kill switch ends the task stopped, killed")
def test_the_kill_switch_ends_stopped_killed() -> None:
    script = Script(answer=lambda _ask: "killed")
    end = run(script)
    assert (end.kind, end.status, end.token) == ("ended", "stopped", "killed")
    assert script.dispatched == []


@pending("§8.3 only an exact yes for the open ask consents")
@pytest.mark.parametrize("answer", ["y", "Yes", "yes ", "", "yes:d-000000000000", "ok"])
def test_anything_but_an_exact_yes_never_dispatches(answer: str) -> None:
    script = Script(answer=lambda _ask: answer)
    run(script)
    assert script.dispatched == []
    assert "write_ahead" not in script.events


@pending("[V2.15] Q6: a yes is used once per action key")
def test_a_yes_for_an_earlier_decision_never_dispatches_again() -> None:
    first = Script()
    consents = Consents()
    run(first, consents=consents)
    earlier = first.asks[0].decision_id
    second = Script(answer=lambda _ask: f"yes:{earlier}")
    run(second, consents=consents)
    assert second.dispatched == []


@pending("§3.4 step 9 screen_changed_during_ask ([v2.24])")
@pytest.mark.parametrize("token", ["screen_changed_during_ask", "not_foreground"])
def test_a_failed_after_yes_check_refuses_and_dispatches_nothing(token: RefusalToken) -> None:
    script = Script(after=token)
    end = run(script)
    assert (end.kind, end.token) == ("refused", token)
    assert "write_ahead" not in script.events
    assert script.dispatched == []


@pending("§8.4: a failed write-ahead dispatches nothing and ends blocked")
def test_a_failed_write_ahead_ends_blocked_wal_unavailable() -> None:
    script = Script(written=False)
    end = run(script)
    assert (end.kind, end.status, end.token) == ("ended", "blocked", "wal_unavailable")
    assert script.dispatched == []


@pending("§8.4: the dispatch carries the decision bound to the action key")
def test_the_dispatched_decision_is_bound_to_the_action_key() -> None:
    script = Script()
    run(script)
    assert script.dispatched[0].action_key == KEY


@pending("§8.5: generated text at R2 asks and says the agent wrote it")
def test_generated_text_is_asked_with_the_generated_reason() -> None:
    script = Script()
    run(script, dataclasses.replace(REQUEST, text_from="agent"))
    assert "generated_text" in script.asks[0].reasons


# ---- the goal check ([V2.15] Q5, packet §3.5) -----------------------------------------------


@pending("[V2.15] Q5: the goal check never approves; serves still asks")
def test_a_serving_verdict_still_asks() -> None:
    script = Script(verdict="serves")
    run(script)
    assert len(script.asks) == 1


@pending("[V2.15] Q5: contrary asks with goal_contrary, never blocks")
def test_a_contrary_verdict_asks_and_a_yes_still_dispatches() -> None:
    script = Script(verdict="contrary")
    end = run(script)
    assert "goal_contrary" in script.asks[0].reasons
    assert end.kind == "dispatched"


@pending("[V2.15] Q5: every verdict asks, none blocks")
@pytest.mark.parametrize("verdict", ["serves", "unrelated", "contrary", "unknown"])
def test_no_verdict_blocks(verdict: Verdict) -> None:
    script = Script(verdict=verdict)
    end = run(script)
    assert end.kind == "dispatched"


@pending("§3.5 the reason token per verdict")
@pytest.mark.parametrize(
    ("verdict", "reasons"),
    [
        ("serves", ()),
        ("unrelated", ("goal_unrelated",)),
        ("contrary", ("goal_contrary",)),
        ("unknown", ("goal_unknown",)),
    ],
)
def test_each_verdict_maps_to_its_reason(verdict: Verdict, reasons: tuple[str, ...]) -> None:
    assert goal_reasons(verdict) == reasons


@pending("§3.5: the goal check sees one row, never the listing or the history")
def test_the_goal_row_holds_only_the_action_facts() -> None:
    row = goal_row(act("click", target=SEND.id), SEND, text=None, text_from=None)
    assert set(dataclasses.asdict(row)) == {"verb", "commit", "role", "name", "text", "text_from"}
    assert (row.verb, row.role, row.name) == ("click", SEND.role, SEND.name)


@pending("§3.5: the goal check reuses v1's goal-match system prompt unchanged")
def test_the_goal_prompt_keeps_the_v1_system_prompt_and_fences_the_row() -> None:
    row = GoalRow(
        verb="click", commit="send", role="Button", name="Send", text=None, text_from=None
    )
    system, user = goal_prompt(GOAL, row, tag="t1")
    assert system == build_prompt(GOAL, [], tag="t1")[0]
    assert "<steps-t1>" in user
    assert "Send" in user


@pending("§3.4 step 6: the goal check gets the row of the action asked about")
def test_the_goal_check_sees_the_asked_actions_row() -> None:
    script = Script()
    run(script)
    assert (script.rows[0].role, script.rows[0].name) == ("Button", "Send")
