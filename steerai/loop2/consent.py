"""Consent in the v2 loop, `bootstrap` mode (V2 §7.2, §8.3, §8.4; [V2.15] Q2, Q5, Q6; packet
v2-step4-claims §3.3 to §3.5): which subgoals are commits, the gate pass for an R2/R3 action, the
answer rule, and the per-action goal check.

Interface stub for step 4; the tests in `tests/loop2/test_invariants_consent.py` and
`tests/loop2/test_invariants_commit.py` come first.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from steerai.action.dispositions import ActionResult
from steerai.goal_match import Verdict
from steerai.loop2._stub import stub
from steerai.loop2.claims import Sent
from steerai.loop2.decision import ActionDoc
from steerai.loop2.task import Commit, GoalSpan
from steerai.orchestrator.contracts import GateDecision, Risk
from steerai.orchestrator.gate import Ask
from steerai.orchestrator.ledger import Admission
from steerai.world.elements import Element

if TYPE_CHECKING:  # the runner imports `history`, which imports this module's token types
    from steerai.loop2.runner import RefusalKind

CommitToken = Literal[
    "commit_conflict",
    "commit_undeclared",
    "not_in_step",
    "no_strict_shape",
    "send_not_typed",
    "composer_differs",
    "composer_unreadable",
]
"""The refusals before any ask ([V2.90] (1)). `no_strict_shape` is a step limit (no strike): a
declared shape beyond step 4. `send_not_typed` is a model fault: the content's pending-text
record is missing or holds another span or generated text. `composer_differs` and
`composer_unreadable` are a failed read-back of that record, world facts; nothing is sent."""
RefusalToken = Literal[
    "commit_conflict",
    "commit_undeclared",
    "not_in_step",
    "no_strict_shape",
    "send_not_typed",
    "composer_differs",
    "composer_unreadable",
    "done_already",
    "screen_changed_during_ask",
    "not_foreground",
]
EndReason = Literal[
    "user_declined", "unanswered", "killed", "wal_unavailable", "takeover", "other_steer_input"
]
InputClass = Literal["takeover", "other_steer_input"]
"""[V2.58] (2): who made an input, as the monitor classes it (`hook_classifier.TAKEOVER_TOKENS`)."""
PostCheck = Literal["none", "query_outcome", "strict_claim"]
TextFrom = Literal["request", "agent"]


@stub(4)
def effective_commit(declared: Commit, dispatched: Sequence[Commit]) -> Commit:
    """A subgoal's commit: its declared one, raised by the declared commit of every action
    dispatched for it (§3.3)."""
    raise NotImplementedError("step 4: the effective commit")


WordClass = Literal["deny", "control", "consent", "billing"]
"""The gate's word classes: `DENY_PATTERNS`, `CONTROL_PATTERNS`, `CONSENT_PATTERNS`,
`BILLING_PATTERNS`."""


@stub(4)
def commit_refusal(  # noqa: PLR0913 - the closed facts of §3.3, keyword-only
    *,
    subgoal_commit: Commit,
    prior: Sequence[Commit],
    action_commit: Commit,
    verb: str,
    raised_by: WordClass | None,
    params: Mapping[str, GoalSpan],
    pending: Sent | None,
) -> CommitToken | None:
    """§3.3, before any ask: two non-`none` kinds on one subgoal (`commit_conflict`); a raise
    with declared commit `none` (`commit_undeclared`) by a deny word on any verb, or by a
    control, consent or billing word on a click or a key press ([V2.17] item 1; on a state verb
    those classes ask and the live read proves the state); a delete or pay (`not_in_step`); a
    send or submit whose subgoal does not have exactly one param, the content ([V2.88] L2;
    `no_strict_shape`, a step limit, [V2.90] (1)); else one whose `pending`, the content
    field's pending-text record, is missing or holds another span or generated text
    (`send_not_typed`, a model fault; [V2.88] L3: the label on the `type` is ignored); else a
    digest read back that differs (`composer_differs`) or was unread (`composer_unreadable`),
    both world facts ([V2.90] (1)). `prior` are the declared commits of the subgoal's
    dispatched actions; `params` are the subgoal's params."""
    raise NotImplementedError("step 4: commit checks")


@stub(4)
def refusal_kind(token: RefusalToken, *, commit_on_row: bool) -> RefusalKind:
    """[V2.74]: a gate refusal's kind, as the token declares it in `runner.REFUSAL_KIND` (no
    default): `not_foreground`, `screen_changed_during_ask`, `composer_unreadable` and
    `composer_differs` are `world`, `done_already` a `model` fault. [V2.88] L8: except
    `commit_undeclared`, a `model` fault only when the commit fact was on the listing row the
    model acted on (`commit_on_row`), else a `step` limit, with no strike."""
    raise NotImplementedError("step 4: refusal kinds")


@stub(4)
def tier_for(commit: Commit) -> Literal["standard", "strict"]:
    """[V2.15] Q2: strict for a commit subgoal, standard otherwise."""
    raise NotImplementedError("step 4: the proof tier")


@stub(4)
def needs_write_ahead(risk: Risk) -> bool:
    """§8.4: every R2/R3 dispatch has a record first, a caution-R2 Enter included."""
    raise NotImplementedError("step 4: the write-ahead rule")


@stub(4)
def post_check(*, risk: Risk, subgoal_commit: Commit, classified: bool) -> PostCheck:
    """What judges a dispatched action's effect. `classified`: an Enter or button click the
    query classifier judged (R1 query or caution R2). [V2.15] Q2: a classified action under a
    commit-`none` subgoal takes the §7.2 three-outcome check at any tier; an action under a
    commit subgoal waits for a strict claim; anything else has no post-check."""
    raise NotImplementedError("step 4: the post-check")


class Consents:
    """[V2.15] Q6: the yes answers of one task. A yes is exactly `yes` or `yes:<decision id>` for
    the open ask, the same for R2 and R3; it is bound to one action key and used once."""

    @stub(4)
    def grant(self, *, decision_id: str, action_key: str, answer: str) -> bool:
        """Record `answer` to the open ask `decision_id`; True only for a yes."""
        raise NotImplementedError("step 4: the answer rule")

    @stub(4)
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


@stub(4)
def goal_row(
    action: ActionDoc, element: Element, *, text: str | None, text_from: TextFrom | None
) -> GoalRow:
    raise NotImplementedError("step 4: the goal row")


@stub(4)
def goal_prompt(goal: str, row: GoalRow, *, tag: str) -> tuple[str, str]:
    """(system, user) for one numbered row, with v1's goal-match system prompt unchanged."""
    raise NotImplementedError("step 4: the goal prompt")


@stub(4)
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
    reasons: tuple[str, ...] = ()
    """The closed reason tokens code computed from the risk and the form facts (e.g.
    `submit_rule`), shown on the ask."""


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
    foreground_at_ask: bool
    """[V2.88] L4: whether the bound window was in front just before the ask was shown."""
    raise_window: Callable[[], bool]
    """[V2.88] L4, the narrow exception to never-raise: called only after a yes and only when
    `foreground_at_ask`, at most once per pass, before the recheck (so never after a failed
    one). True when the same window handle is in front after it; False (another handle) is
    `not_foreground`, and nothing is written or dispatched."""
    after_yes: Callable[[], GateRequest | RefusalToken]
    """Step 9: the fresh observe: the asked element, the foreground and the value digest again
    (a token when one fails), else the request recomputed on it, risk and form facts included.
    [V2.88] L4: the yes is consumed only when that request equals the one the ask showed; any
    difference, in tier or ask reasons too, is `screen_changed_during_ask`."""
    write_ahead: Callable[[GateDecision], bool]
    """Step 10: False when the record could not be written."""
    input_since_answer: Callable[[], InputClass | None]
    """[V2.88] L5, [V2.90] (3): the monitor's class of any input since the answer that is not
    the ask bracket's own (`input_after_answer`); None when there is none. Read just before the
    dispatch, and before a failed after-yes check or raise is reported: a takeover is never
    relabelled, so any input ends the task with its class (stopped), never as that refusal."""
    void: Callable[[GateDecision], None]
    """[V2.90] (3): closes the record written for `decision` as not delivered. Called only on
    the branch where the dispatch call was never made (input after the write-ahead); never
    inferred at the next start, where an open record refuses. The yes is consumed by it: a new
    attempt needs a new ask."""
    dispatch: Callable[[GateDecision], ActionResult]
    """Step 11, returning v1's result. [V2.25] (c): a `not_delivered` with v1's retry codes and
    no step tried gets v1's one local retry under the SAME decision: one ask, one write-ahead,
    a second dispatch only (v1 `orchestrator/loop.py` `_act`); an uncertain one is never re-sent."""


@dataclass(frozen=True)
class GateEnd:
    """How one gate pass ended: dispatched; refused with a token (and a strike); or the task
    ends with a status and a reason."""

    kind: Literal["dispatched", "refused", "ended"]
    token: RefusalToken | EndReason | None
    status: Literal["blocked", "paused", "stopped"] | None


@dataclass(frozen=True)
class InputEvent:
    """One low-level input event as the monitor saw it ([V2.88] L5), in arrival order."""

    key: int | None
    """The virtual-key code; None for a pointer event."""
    down: bool
    in_bracket: bool
    """Seen while the ask bracket was open: input to Steer's own ask window."""


@stub(4)
def input_after_answer(events: Sequence[InputEvent]) -> bool:
    """[V2.88] L5, [V2.90] (3): whether `events` hold input after the answer: any event outside
    the bracket, except a key-up whose key-down was inside it (the answer key's own release can
    arrive after the bracket closes; it belongs to the bracket and is never input)."""
    raise NotImplementedError("step 4: the ask bracket")


@stub(4)
def gate_pass(
    request: GateRequest, steps: GateSteps, consents: Consents, *, task_id: str, goal: str
) -> GateEnd:
    """§3.4 in order: checks, admission (`done` refuses `done_already`; `confirm` asks with
    `previous_attempt`), goal check, ask, answer (`no` ends blocked `user_declined`; `killed`
    ends stopped `killed`; anything else but a yes ends paused `unanswered`), the raise
    ([V2.88] L4, only after a yes and only when the window was in front before the ask), the
    after-yes checks (a failed after-yes check or raise ends the task stopped with the input's
    class when there was input since the answer, never relabelled), write-ahead (a failed
    write ends blocked `wal_unavailable`), the input check (any input since the answer ends
    the task stopped with its class, voids the record and dispatches nothing), one dispatch.
    [V2.88] L2: a commit ask shows the full request (`goal`) and the closed reason
    `recipient_unchecked` (Steer did not check the recipient)."""
    raise NotImplementedError("step 4: the gate pass")
