"""The gate ask inside the v2 loop, `bootstrap` mode (V2 §7.2, §8.3, §8.4, §8.5; packet
v2-step4-claims §3.4, §3.5; [V2.15] Q5, Q6, Q7), refreshed to the refusal kinds ([V2.74]) and
[V2.88]: the full request and the unchecked recipient on the ask (L2), the yes consumed only when
the recheck matches what the ask showed (L4), no dispatch after other input (L5), and the
scripted no, timeout and state-change cases (L11); and [V2.90] (3): the void only where the
dispatch was never called, a takeover never relabelled, the answer key's late key-up belonging
to the bracket. Red on the step 4 stub."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import get_args

import pytest

from steerai.action.dispositions import ActionResult, Delivery
from steerai.errors import ErrorCode
from steerai.goal_match import Verdict, build_prompt
from steerai.loop2.consent import (
    Consents,
    GateEnd,
    GateRequest,
    GateSteps,
    GoalRow,
    InputClass,
    InputEvent,
    RefusalToken,
    gate_pass,
    goal_prompt,
    goal_reasons,
    goal_row,
    input_after_answer,
    refusal_kind,
)
from steerai.loop2.runner import REFUSAL_KIND
from steerai.loop2.task import Commit
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
    recheck: GateRequest | None = None
    """The request recomputed on the fresh observe after the yes; None: the one asked about."""
    input: InputClass | None = None
    """[V2.88] L5: the monitor's class of input since the answer that is not the bracket's."""
    foreground_at_ask: bool = True
    """[V2.88] L4: the bound window was in front just before the ask."""
    raised: bool = True
    """[V2.88] L4: the same window handle is in front after the raise."""
    written: bool = True
    request: GateRequest = field(default_factory=lambda: REQUEST)
    events: list[str] = field(default_factory=list)
    asks: list[Ask] = field(default_factory=list)
    rows: list[GoalRow] = field(default_factory=list)
    dispatched: list[GateDecision] = field(default_factory=list)
    results: list[ActionResult] = field(default_factory=list)
    """What each dispatch returns, in order (then delivered and verified)."""

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

    def _raise(self) -> bool:
        self.events.append("raise_window")
        return self.raised

    def _after(self) -> GateRequest | RefusalToken:
        self.events.append("after_yes")
        if self.after is not None:
            return self.after
        return self.request if self.recheck is None else self.recheck

    def _write(self, decision: GateDecision) -> bool:  # noqa: ARG002 - the GateSteps shape
        self.events.append("write_ahead")
        return self.written

    def _input(self) -> InputClass | None:
        self.events.append("input_check")
        return self.input

    def _void(self, decision: GateDecision) -> None:  # noqa: ARG002 - the GateSteps shape
        self.events.append("void")

    def _dispatch(self, decision: GateDecision) -> ActionResult:
        self.events.append("dispatch")
        self.dispatched.append(decision)
        if self.results:
            return self.results.pop(0)
        return ActionResult(verb="click", delivery=Delivery.DELIVERED_VERIFIED)

    def steps(self) -> GateSteps:
        return GateSteps(
            checks=self._checks,
            admit=self._admit,
            goal_check=self._goal,
            ask=self._ask,
            foreground_at_ask=self.foreground_at_ask,
            raise_window=self._raise,
            after_yes=self._after,
            write_ahead=self._write,
            input_since_answer=self._input,
            void=self._void,
            dispatch=self._dispatch,
        )


def run(
    script: Script, request: GateRequest = REQUEST, consents: Consents | None = None
) -> GateEnd:
    script.request = request
    return gate_pass(request, script.steps(), consents or Consents(), task_id="t", goal=GOAL)


LEFT = ActionResult(verb="click", delivery=Delivery.NOT_DELIVERED, code=ErrorCode.ELEMENT_NOT_FOUND)
UNCERTAIN = ActionResult(
    verb="click", delivery=Delivery.DELIVERY_UNCERTAIN, code=ErrorCode.VERIFY_TIMEOUT
)
"""v1's results for nothing sent (one local retry) and an uncertain delivery ([V2.25] (c))."""


ORDER = [
    "checks",
    "admit",
    "goal_check",
    "ask",
    "raise_window",
    "after_yes",
    "write_ahead",
    "input_check",
    "dispatch",
]


@pending("§3.4, [V2.88] L4, L5: checks to dispatch, raise and input check included", step=4)
def test_a_yes_runs_every_step_in_order_and_dispatches_once() -> None:
    script = Script()
    end = run(script)
    assert (end.kind, script.events) == ("dispatched", ORDER)
    assert len(script.dispatched) == 1


@pending("§8.3, [V2.15] Q6: the same answer rule for R2 and R3", step=4)
@pytest.mark.parametrize("risk", ["R2", "R3"])
def test_every_r2_and_r3_action_asks_in_bootstrap_mode(risk: Risk) -> None:
    script = Script()
    run(script, dataclasses.replace(REQUEST, risk=risk))
    assert len(script.asks) == 1
    assert script.asks[0].risk == risk


@pending("§3.4 steps 3 and 4: a refused check asks nothing and dispatches nothing", step=4)
@pytest.mark.parametrize(
    "token", ["commit_undeclared", "no_strict_shape", "not_in_step", "composer_differs"]
)
def test_a_failed_check_refuses_before_the_ledger_and_the_ask(token: RefusalToken) -> None:
    script = Script(checks=token)
    end = run(script)
    assert (end.kind, end.token) == ("refused", token)
    assert script.events == ["checks"]


@pending("§8.4, §3.4 step 5: a key closed done refuses done_already with no ask", step=4)
def test_a_done_key_refuses_done_already_without_a_goal_check_or_an_ask() -> None:
    script = Script(admission="done")
    end = run(script)
    assert (end.kind, end.token) == ("refused", "done_already")
    assert script.events == ["checks", "admit"]


@pending("§8.4: an unknown outcome confirms, never re-sends", step=4)
def test_a_key_after_an_unknown_outcome_asks_with_previous_attempt() -> None:
    script = Script(admission="confirm")
    run(script)
    assert script.asks[0].previous_attempt is not None


@pending("§8.4: an unknown outcome confirms, never re-sends", step=4)
def test_a_key_after_an_unknown_outcome_never_dispatches_without_a_new_yes() -> None:
    script = Script(admission="confirm", answer=lambda _ask: "no")
    run(script)
    assert script.dispatched == []


@pending("§3.4 step 8: a no ends the task blocked, user_declined", step=4)
@pytest.mark.parametrize("answer", ["no", "no:{id}"])
def test_a_no_ends_blocked_user_declined(answer: str) -> None:
    script = Script(answer=lambda ask: answer.format(id=ask.decision_id))
    end = run(script)
    assert (end.kind, end.status, end.token) == ("ended", "blocked", "user_declined")
    assert "write_ahead" not in script.events
    assert script.dispatched == []


@pending("§3.4 step 8, [V2.15] Q7: no answer ends the task paused, unanswered", step=4)
def test_no_answer_ends_paused_unanswered() -> None:
    script = Script(answer=lambda _ask: "unanswered")
    end = run(script)
    assert (end.kind, end.status, end.token) == ("ended", "paused", "unanswered")
    assert script.dispatched == []


@pending("§3.4 step 8: the kill switch ends the task stopped, killed", step=4)
def test_the_kill_switch_ends_stopped_killed() -> None:
    script = Script(answer=lambda _ask: "killed")
    end = run(script)
    assert (end.kind, end.status, end.token) == ("ended", "stopped", "killed")
    assert script.dispatched == []


@pending("§8.3 only an exact yes for the open ask consents", step=4)
@pytest.mark.parametrize("answer", ["y", "Yes", "yes ", "", "yes:d-000000000000", "ok"])
def test_anything_but_an_exact_yes_never_dispatches(answer: str) -> None:
    script = Script(answer=lambda _ask: answer)
    run(script)
    assert script.dispatched == []
    assert "write_ahead" not in script.events


@pending("[V2.15] Q6: a yes is used once per action key", step=4)
def test_a_yes_for_an_earlier_decision_never_dispatches_again() -> None:
    first = Script()
    consents = Consents()
    run(first, consents=consents)
    earlier = first.asks[0].decision_id
    second = Script(answer=lambda _ask: f"yes:{earlier}")
    run(second, consents=consents)
    assert second.dispatched == []


@pending("§3.4 step 9 screen_changed_during_ask ([v2.24])", step=4)
@pytest.mark.parametrize("token", ["screen_changed_during_ask", "not_foreground"])
def test_a_failed_after_yes_check_refuses_and_dispatches_nothing(token: RefusalToken) -> None:
    script = Script(after=token)
    end = run(script)
    assert (end.kind, end.token) == ("refused", token)
    assert "write_ahead" not in script.events
    assert script.dispatched == []


@pending("[V2.88] L4: risk and form facts are recomputed after the yes", step=4)
@pytest.mark.parametrize(
    "recheck",
    [
        dataclasses.replace(REQUEST, risk="R3"),
        dataclasses.replace(REQUEST, reasons=("secure_type",)),
        dataclasses.replace(REQUEST, name="Send all"),
        dataclasses.replace(REQUEST, text="red boots"),
        dataclasses.replace(REQUEST, window="Other"),
    ],
    ids=["tier", "ask_reasons", "name", "text", "window"],
)
def test_any_change_from_what_the_ask_showed_is_screen_changed_during_ask(
    recheck: GateRequest,
) -> None:
    script = Script(recheck=recheck)
    end = run(script)
    assert (end.kind, end.token) == ("refused", "screen_changed_during_ask")
    assert "write_ahead" not in script.events
    assert script.dispatched == []


@pending("[V2.88] L4: a yes is consumed only by a dispatch whose recheck matched", step=4)
def test_a_yes_voided_by_a_changed_recheck_is_never_spent_later() -> None:
    consents = Consents()
    script = Script(recheck=dataclasses.replace(REQUEST, risk="R3"))
    run(script, consents=consents)
    asked = script.asks[0].decision_id
    assert consents.spend(decision_id=asked, action_key=KEY) is False


@pending("[V2.90] (3): input after the answer ends the task as a takeover, record voided", step=4)
@pytest.mark.parametrize("who", ["takeover", "other_steer_input"])
def test_input_after_the_answer_ends_as_a_takeover_and_voids_the_record(who: InputClass) -> None:
    script = Script(input=who)
    end = run(script)
    assert (end.kind, end.token, end.status) == ("ended", who, "stopped")
    assert script.events[-2:] == ["input_check", "void"]
    assert "dispatch" not in script.events
    assert script.dispatched == []


@pending("[V2.90] (3): a takeover is never relabelled as a changed screen", step=4)
def test_a_failed_recheck_with_input_since_the_answer_ends_as_a_takeover() -> None:
    script = Script(recheck=dataclasses.replace(REQUEST, name="Send all"), input="takeover")
    end = run(script)
    assert (end.kind, end.token, end.status) == ("ended", "takeover", "stopped")
    assert "write_ahead" not in script.events
    assert "void" not in script.events, "nothing was written, so nothing is voided"
    assert script.dispatched == []


@pending("[V2.90] (3): screen_changed_during_ask only for a differing recheck, no input", step=4)
def test_a_failed_recheck_without_input_is_screen_changed_during_ask() -> None:
    script = Script(recheck=dataclasses.replace(REQUEST, name="Send all"), input=None)
    end = run(script)
    assert (end.kind, end.token) == ("refused", "screen_changed_during_ask")
    assert REFUSAL_KIND["screen_changed_during_ask"] == "world"


@pending("[V2.90] (3): a voided yes is consumed, a new attempt needs a new ask", step=4)
def test_a_new_attempt_after_a_void_needs_a_new_ask() -> None:
    consents = Consents()
    first = Script(input="takeover")
    run(first, consents=consents)
    earlier = first.asks[0].decision_id
    assert consents.spend(decision_id=earlier, action_key=KEY) is False
    again = Script(answer=lambda _ask: f"yes:{earlier}")
    run(again, consents=consents)
    assert again.dispatched == []
    assert again.events.count("ask") == 1, "the new attempt asks again"


@pending("[V2.90] (3): the void is written only where the dispatch was never called", step=4)
@pytest.mark.parametrize(
    "script",
    [
        Script(),
        Script(results=[LEFT]),
        Script(results=[UNCERTAIN]),
        Script(after="not_foreground"),
        Script(written=False),
        Script(answer=lambda _ask: "no"),
    ],
    ids=["delivered", "retried", "uncertain", "after_yes_failed", "write_failed", "declined"],
)
def test_no_void_once_the_dispatch_was_called_or_nothing_was_written(script: Script) -> None:
    run(script)
    assert "void" not in script.events


# ---- the ask bracket ([V2.88] L5, [V2.90] (3)) -----------------------------------------------

ENTER = 0x0D
A_KEY = 0x41


@pending("[V2.90] (3): the answer key's key-up after the bracket closes is the bracket's", step=4)
def test_the_answer_keys_late_key_up_is_not_input() -> None:
    events = [InputEvent(ENTER, down=True, in_bracket=True), InputEvent(ENTER, False, False)]
    assert input_after_answer(events) is False


@pending("[V2.88] L5: a key pressed after the bracket closes is input", step=4)
def test_a_key_down_after_the_bracket_is_input() -> None:
    events = [InputEvent(ENTER, down=True, in_bracket=True), InputEvent(A_KEY, True, False)]
    assert input_after_answer(events) is True


@pending("[V2.88] L5: a key-up of a key never pressed in the bracket is input", step=4)
def test_a_key_up_of_another_key_after_the_bracket_is_input() -> None:
    events = [InputEvent(ENTER, down=True, in_bracket=True), InputEvent(A_KEY, False, False)]
    assert input_after_answer(events) is True


@pending("[V2.88] L5: a pointer event after the bracket closes is input", step=4)
def test_a_pointer_event_after_the_bracket_is_input() -> None:
    events = [InputEvent(ENTER, down=True, in_bracket=True), InputEvent(None, True, False)]
    assert input_after_answer(events) is True


@pending("[V2.88] L5: input to Steer's own ask window is the bracket's", step=4)
def test_input_inside_the_bracket_is_not_input() -> None:
    events = [InputEvent(A_KEY, True, True), InputEvent(A_KEY, False, True)]
    assert input_after_answer(events) is False


# ---- the raise after a yes ([V2.88] L4) ------------------------------------------------------


@pending("[V2.88] L4: the window is raised once, only after a yes", step=4)
def test_a_yes_raises_the_window_once_before_the_recheck() -> None:
    script = Script()
    run(script)
    assert script.events.count("raise_window") == 1
    assert script.events.index("raise_window") == script.events.index("ask") + 1
    assert script.events.index("raise_window") < script.events.index("after_yes")


@pending("[V2.88] L4: no raise without a yes", step=4)
@pytest.mark.parametrize("answer", ["no", "unanswered", "killed", "y"])
def test_no_yes_never_raises_the_window(answer: str) -> None:
    script = Script(answer=lambda _ask: answer)
    run(script)
    assert "raise_window" not in script.events


@pending("[V2.88] L4: no raise when the window was not in front before the ask", step=4)
def test_a_window_not_in_front_before_the_ask_is_never_raised() -> None:
    script = Script(foreground_at_ask=False)
    run(script)
    assert "raise_window" not in script.events


@pending("[V2.88] L4: no second raise, and none after a failed recheck", step=4)
@pytest.mark.parametrize(
    "script",
    [
        Script(after="screen_changed_during_ask"),
        Script(after="not_foreground"),
        Script(recheck=dataclasses.replace(REQUEST, risk="R3")),
    ],
    ids=["screen_changed", "not_foreground", "recheck_differs"],
)
def test_a_failed_recheck_is_never_followed_by_a_raise(script: Script) -> None:
    run(script)
    assert script.events.count("raise_window") == 1
    assert "raise_window" not in script.events[script.events.index("after_yes") :]
    assert script.dispatched == []


@pending("[V2.88] L4: another window handle in front after the raise is not_foreground", step=4)
def test_a_different_handle_after_the_raise_is_not_foreground() -> None:
    script = Script(raised=False)
    end = run(script)
    assert (end.kind, end.token) == ("refused", "not_foreground")
    assert script.events.count("raise_window") == 1
    assert "write_ahead" not in script.events
    assert script.dispatched == []


@pending("[V2.88] L11: the scripted no, timeout and state change each dispatch nothing", step=4)
@pytest.mark.parametrize(
    "script",
    [
        Script(answer=lambda ask: f"no:{ask.decision_id}"),
        Script(answer=lambda _ask: "unanswered"),
        Script(recheck=dataclasses.replace(REQUEST, name="Delete")),
    ],
    ids=["no", "timeout", "state_change_after_ask"],
)
def test_a_scripted_no_timeout_or_state_change_dispatches_nothing(script: Script) -> None:
    end = run(script)
    assert end.kind != "dispatched"
    assert script.dispatched == []
    assert "write_ahead" not in script.events


@pending("[V2.88] L2: the ask shows the full request and the unchecked recipient", step=4)
@pytest.mark.parametrize("commit", ["send", "submit"])
def test_a_commit_ask_shows_the_full_request_and_that_the_recipient_was_not_checked(
    commit: Commit,
) -> None:
    script = Script()
    run(script, dataclasses.replace(REQUEST, commit=commit))
    ask = script.asks[0]
    assert GOAL in ask.params.values()
    assert "recipient_unchecked" in ask.reasons


@pending("§8.4: a failed write-ahead dispatches nothing and ends blocked", step=4)
def test_a_failed_write_ahead_ends_blocked_wal_unavailable() -> None:
    script = Script(written=False)
    end = run(script)
    assert (end.kind, end.status, end.token) == ("ended", "blocked", "wal_unavailable")
    assert script.dispatched == []


@pending("§8.4: the dispatch carries the decision bound to the action key", step=4)
def test_the_dispatched_decision_is_bound_to_the_action_key() -> None:
    script = Script()
    run(script)
    assert script.dispatched[0].action_key == KEY


@pending("§8.5: generated text at R2 asks and says the agent wrote it", step=4)
def test_generated_text_is_asked_with_the_generated_reason() -> None:
    script = Script()
    run(script, dataclasses.replace(REQUEST, text_from="agent"))
    assert "generated_text" in script.asks[0].reasons


# ---- refusal kinds ([V2.74]) -----------------------------------------------------------------

WORLD = ("not_foreground", "screen_changed_during_ask", "composer_unreadable", "composer_differs")


def test_every_gate_refusal_token_declares_its_kind() -> None:
    """[V2.74]: no default kind; a token without one is the missing-fact-read-as-safe error."""
    assert sorted(set(get_args(RefusalToken)) - set(REFUSAL_KIND)) == []


def test_the_world_facts_at_the_ask_are_world_and_done_already_is_a_model_fault() -> None:
    assert {t: REFUSAL_KIND[t] for t in WORLD} == dict.fromkeys(WORLD, "world")
    assert REFUSAL_KIND["done_already"] == "model"


@pending("[V2.74], [V2.88] L8: every gate refusal's kind is the table's but one", step=4)
@pytest.mark.parametrize("token", get_args(RefusalToken))
@pytest.mark.parametrize("on_row", [True, False])
def test_every_refusal_kind_is_the_declared_one_but_an_off_row_commit_undeclared(
    token: RefusalToken, on_row: bool
) -> None:
    """Total over the tokens: the declared kind, except `commit_undeclared` with the commit fact
    off the acted row, a step limit (`test_invariants_commit.py`)."""
    expected = "step" if (token, on_row) == ("commit_undeclared", False) else REFUSAL_KIND[token]
    assert refusal_kind(token, commit_on_row=on_row) == expected


# ---- the goal check ([V2.15] Q5, packet §3.5) -----------------------------------------------


@pending("[V2.15] Q5: the goal check never approves; serves still asks", step=4)
def test_a_serving_verdict_still_asks() -> None:
    script = Script(verdict="serves")
    run(script)
    assert len(script.asks) == 1


@pending("[V2.15] Q5: contrary asks with goal_contrary, never blocks", step=4)
def test_a_contrary_verdict_asks_and_a_yes_still_dispatches() -> None:
    script = Script(verdict="contrary")
    end = run(script)
    assert "goal_contrary" in script.asks[0].reasons
    assert end.kind == "dispatched"


@pending("[V2.15] Q5: every verdict asks, none blocks", step=4)
@pytest.mark.parametrize("verdict", ["serves", "unrelated", "contrary", "unknown"])
def test_no_verdict_blocks(verdict: Verdict) -> None:
    script = Script(verdict=verdict)
    end = run(script)
    assert end.kind == "dispatched"


@pending("§3.5 the reason token per verdict", step=4)
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


@pending("§3.5: the goal check sees one row, never the listing or the history", step=4)
def test_the_goal_row_holds_only_the_action_facts() -> None:
    row = goal_row(act("click", target=SEND.id), SEND, text=None, text_from=None)
    assert set(dataclasses.asdict(row)) == {"verb", "commit", "role", "name", "text", "text_from"}
    assert (row.verb, row.role, row.name) == ("click", SEND.role, SEND.name)


@pending("§3.5: the goal check reuses v1's goal-match system prompt unchanged", step=4)
def test_the_goal_prompt_keeps_the_v1_system_prompt_and_fences_the_row() -> None:
    row = GoalRow(
        verb="click", commit="send", role="Button", name="Send", text=None, text_from=None
    )
    system, user = goal_prompt(GOAL, row, tag="t1")
    assert system == build_prompt(GOAL, [], tag="t1")[0]
    assert "<steps-t1>" in user
    assert "Send" in user


@pending("§3.4 step 6: the goal check gets the row of the action asked about", step=4)
def test_the_goal_check_sees_the_asked_actions_row() -> None:
    script = Script()
    run(script)
    assert (script.rows[0].role, script.rows[0].name) == ("Button", "Send")


# ---- [V2.25] (c): v1's one local retry reuses the record and the answer ----------------------


@pending("[V2.25] (c), §8.4: nothing sent is retried once under the same record and answer", step=4)
@pytest.mark.parametrize("risk", ["R2", "R3"])
def test_nothing_sent_is_retried_once_with_no_second_ask_or_write_ahead(risk: Risk) -> None:
    script = Script(results=[LEFT])
    end = run(script, dataclasses.replace(REQUEST, risk=risk))
    assert end.kind == "dispatched"
    assert script.events.count("ask") == 1, "no second answer token"
    assert script.events.count("write_ahead") == 1, "no second write-ahead for the same key"
    assert script.events.count("dispatch") == 2
    assert script.dispatched[0] == script.dispatched[1], "the same decision"


@pending("[V2.25] (c): v1's local retry is once", step=4)
def test_the_local_retry_is_once() -> None:
    script = Script(results=[LEFT, LEFT])
    run(script)
    assert script.events.count("dispatch") == 2
    assert script.events.count("write_ahead") == 1


@pending("[V2.25] (c), §8.4: an uncertain delivery is never re-sent", step=4)
def test_an_uncertain_delivery_is_never_re_sent() -> None:
    script = Script(results=[UNCERTAIN])
    run(script)
    assert script.events.count("dispatch") == 1
