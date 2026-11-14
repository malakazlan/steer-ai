"""Consent in the v2 loop, `bootstrap` mode (V2 §7.2, §8.3, §8.4; [V2.15] Q2, Q5, Q6; packet
v2-step4-claims §3.3 to §3.5): which subgoals are commits, the gate pass for an R2/R3 action, the
answer rule, and the per-action goal check.

Interface stub for step 4; the tests in `tests/loop2/test_invariants_consent.py` and
`tests/loop2/test_invariants_commit.py` come first.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from steerai.goal_match import Verdict
from steerai.loop2._stub import stub
from steerai.loop2.decision import ActionDoc
from steerai.loop2.task import Commit
from steerai.orchestrator.contracts import GateDecision, Risk
from steerai.orchestrator.gate import Ask
from steerai.orchestrator.ledger import Admission
from steerai.world.elements import Element

CommitToken = Literal["commit_conflict", "commit_undeclared", "not_in_step", "no_strict_shape"]
RefusalToken = Literal[
    "commit_conflict",
    "commit_undeclared",
    "not_in_step",
    "no_strict_shape",
    "composer_differs",
    "composer_unreadable",
    "done_already",
    "screen_changed_during_ask",
    "not_foreground",
]
EndReason = Literal["user_declined", "unanswered", "killed", "wal_unavailable"]
PostCheck = Literal["none", "query_outcome", "strict_claim"]
TextFrom = Literal["request", "agent"]


@stub
def effective_commit(declared: Commit, dispatched: Sequence[Commit]) -> Commit:
    """A subgoal's commit: its declared one, raised by the declared commit of every action
    dispatched for it (§3.3)."""
    raise NotImplementedError("step 4: the effective commit")


WordClass = Literal["deny", "control", "consent", "billing"]
"""The gate's word classes: `DENY_PATTERNS`, `CONTROL_PATTERNS`, `CONSENT_PATTERNS`,
`BILLING_PATTERNS`."""


@stub
def commit_refusal(  # noqa: PLR0913 - the closed facts of §3.3, keyword-only
    *,
    subgoal_commit: Commit,
    prior: Sequence[Commit],
    action_commit: Commit,
    verb: str,
    raised_by: WordClass | None,
    typed_param: str | None,
) -> CommitToken | None:
    """§3.3, before any ask: two non-`none` kinds on one subgoal (`commit_conflict`); a raise
    with declared commit `none` (`commit_undeclared`) by a deny word on any verb, or by a
    control, consent or billing word on a click or a key press ([V2.17] item 1; on a state verb
    those classes ask and the live read proves the state); a delete or pay (`not_in_step`); a
    send or submit with no verified `type` of a subgoal param (`no_strict_shape`). `prior` are
    the declared commits of the subgoal's dispatched actions."""
    raise NotImplementedError("step 4: commit checks")


@stub
def tier_for(commit: Commit) -> Literal["standard", "strict"]:
    """[V2.15] Q2: strict for a commit subgoal, standard otherwise."""
    raise NotImplementedError("step 4: the proof tier")


@stub
def needs_write_ahead(risk: Risk) -> bool:
    """§8.4: every R2/R3 dispatch has a record first, a caution-R2 Enter included."""
    raise NotImplementedError("step 4: the write-ahead rule")


@stub
def post_check(*, risk: Risk, subgoal_commit: Commit, classified: bool) -> PostCheck:
    """What judges a dispatched action's effect. `classified`: an Enter or button click the
    query classifier judged (R1 query or caution R2). [V2.15] Q2: a classified action under a
    commit-`none` subgoal takes the §7.2 three-outcome check at any tier; an action under a
    commit subgoal waits for a strict claim; anything else has no post-check."""
    raise NotImplementedError("step 4: the post-check")


class Consents:
    """[V2.15] Q6: the yes answers of one task. A yes is exactly `yes` or `yes:<decision id>` for
    the open ask, the same for R2 and R3; it is bound to one action key and used once."""

    @stub
    def grant(self, *, decision_id: str, action_key: str, answer: str) -> bool:
        """Record `answer` to the open ask `decision_id`; True only for a yes."""
        raise NotImplementedError("step 4: the answer rule")

    @stub
    def spend(self, *, decision_id: str, action_key: str) -> bool:
        """True once for a granted decision and its own key; False after that, for another
        key, or for a decision never granted."""
        raise NotImplementedError("step 4: single use")


@dataclass(frozen=True)
class GoalRow:
    """[V2.15] §3.5: the one row the goal check sees, beside the request. Never the listing,
    the history or any subgoal text."""

    verb: str
    commit: Commit
    role: str
    name: str
    text: str | None
    text_from: TextFrom | None


@stub
def goal_row(
    action: ActionDoc, element: Element, *, text: str | None, text_from: TextFrom | None
) -> GoalRow:
    raise NotImplementedError("step 4: the goal row")


@stub
def goal_prompt(goal: str, row: GoalRow, *, tag: str) -> tuple[str, str]:
    """(system, user) for one numbered row, with v1's goal-match system prompt unchanged."""
    raise NotImplementedError("step 4: the goal prompt")


@stub
def goal_reasons(verdict: Verdict) -> tuple[str, ...]:
    """[V2.15] Q5: reason tokens for the ask. A verdict never approves and never blocks."""
    raise NotImplementedError("step 4: goal reasons")


@dataclass(frozen=True)
class GateRequest:
    """One R2/R3 action at the gate, as closed facts plus the names the ask shows."""

    action_key: str
    verb: str
    commit: Commit
    risk: Risk
    role: str
    name: str
    text: str | None
    text_from: TextFrom | None
    window: str


@dataclass(frozen=True)
class GateSteps:
    """The steps of §3.4 that touch the world, injected so the order is testable."""

    checks: Callable[[], RefusalToken | None]
    """Steps 3 and 4: the commit checks and the field's value digest."""
    admit: Callable[[str], Admission]
    """Step 5: the ledger's admission for the action key."""
    goal_check: Callable[[GoalRow], Verdict]
    """Step 6."""
    ask: Callable[[Ask], str]
    """Steps 7 and 8: the person's answer token."""
    after_yes: Callable[[], RefusalToken | None]
    """Step 9: the fresh observe, the asked element, the foreground, the value digest again."""
    write_ahead: Callable[[GateDecision], bool]
    """Step 10: False when the record could not be written."""
    dispatch: Callable[[GateDecision], None]
    """Step 11."""


@dataclass(frozen=True)
class GateEnd:
    """How one gate pass ended: dispatched; refused with a token (and a strike); or the task
    ends with a status and a reason."""

    kind: Literal["dispatched", "refused", "ended"]
    token: RefusalToken | EndReason | None
    status: Literal["blocked", "paused", "stopped"] | None


@stub
def gate_pass(
    request: GateRequest, steps: GateSteps, consents: Consents, *, task_id: str, goal: str
) -> GateEnd:
    """§3.4 in order: checks, admission (`done` refuses `done_already`; `confirm` asks with
    `previous_attempt`), goal check, ask, answer (`no` ends blocked `user_declined`; `killed`
    ends stopped `killed`; anything else but a yes ends paused `unanswered`), the after-yes
    checks, write-ahead (a failed write ends blocked `wal_unavailable`), one dispatch."""
    raise NotImplementedError("step 4: the gate pass")
