"""The v2 loop for step 3 (V2 §5), behind `--loop v2`: ports, the resolver, the action risk and
`run_task`.

Interface stub for step 3c. The §8 invariant tests in `tests/loop2/test_invariants_loop.py` are
written first against these shapes and fail with `NotImplementedError` until 3c implements them.
Every outside effect goes through `Ports`, so the tests drive the loop with fakes.

[V2.11]: the Task is built before the first call (goal, budgets, no subgoals); the first valid
Decision fills the subgoals once and code freezes them. When nothing was dispatched the loop
refuses with a token and a strike; the task ends only through strikes, a stop, the budgets or
the gate, and in step 3 a commit ends it `blocked` with `commit_not_in_step`.

[V2.12]: code picks the status after a stop from the latest refusal since the last progress,
never from the model's word. A policy refusal (a secure field, anything the gate would not allow)
ends `blocked` with its token as the reason; a step limit (a claim kind not in step 3) ends
`blocked` with `not_in_step`; a model fault (an unlisted id, a wrong target shape) keeps the
stop's own value and the reason records `after_refusal` plus the token.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from steerai.action.dispositions import ActionResult, Delivery
from steerai.errors import ErrorCode, SteerError
from steerai.loop2._stub import stub
from steerai.loop2.decision import ActionDoc, Decision
from steerai.loop2.guards import StrikeReason
from steerai.loop2.query import SearchFacts
from steerai.loop2.task import Task
from steerai.orchestrator.contracts import Risk
from steerai.world.effects import Observed, ValueFact
from steerai.world.elements import Element
from steerai.world.model import Snapshot, resolve_in
from steerai.world.watch import Watched

Status = Literal["done", "blocked", "asked", "paused", "stopped", "cannot"]
REASON_TOKEN = re.compile(r"[a-z0-9_]{1,64}")


@dataclass(frozen=True)
class Reason:
    """Why a task ended, held as a pair ([V2.13] decision 5): a closed code token, and whether it
    is a model fault recorded after a refusal ([V2.12]). `render_reason` is its only rendering."""

    token: str
    after_refusal: bool = False

    def __post_init__(self) -> None:
        if REASON_TOKEN.fullmatch(self.token) is None:
            raise ValueError("reason_token")


def render_reason(reason: Reason | None) -> str | None:
    """`<token>`, or `after_refusal:<token>` for a model fault; None for no reason."""
    if reason is None:
        return None
    return f"after_refusal:{reason.token}" if reason.after_refusal else reason.token


Provenance = Literal["goal_span", "generated"]


@dataclass(frozen=True)
class Dispatch:
    """One action the loop sends: the model's action, the resolved element and its risk."""

    action: ActionDoc
    runtime_id: tuple[int, ...]
    risk: Risk
    provenance: Provenance | None
    """Set by code, never by the model, for a `type` ([V2.11] decision 5): `param` gives
    `goal_span`; free text is always `generated`, even when it matches the request."""


@dataclass(frozen=True)
class TurnView:
    """What one controller call is given: the latest observation and closed history tokens."""

    observed: Observed
    history: tuple[str, ...]
    """Code-written tokens only (§8.6): never screen text or model text."""
    first_turn: bool


READ_BACK_VERBS = frozenset({"type", "choose", "check", "uncheck", "select", "expand", "collapse"})
"""The verbs whose actuation reads its result back ([V2.11] item 2, [V2.5]): type, choose and the
state verbs."""


@dataclass(frozen=True)
class ActResult:
    """[V2.24] item 3: what one dispatch returns to the loop. `action` is v1's `ActionResult`
    (its delivery, its code, and the method steps, label and mechanism, kept for step 5);
    `read_back` is the three-valued read-back of a read-back verb: True verified, False a
    mismatch, None unread. The read-back belongs to the actuation (§7.3: type, then read back),
    so it travels here and no separate port reads it."""

    action: ActionResult
    read_back: bool | None

    def __post_init__(self) -> None:
        if self.read_back is None:
            return
        if self.action.verb not in READ_BACK_VERBS:
            raise ValueError("read_back is only for type, choose and the state verbs")
        sent_nothing = self.action.delivery is Delivery.NOT_DELIVERED and not self.action.skipped
        if sent_nothing:
            raise ValueError("read_back needs a delivery: nothing was sent")
        verified = self.action.delivery is Delivery.DELIVERED_VERIFIED or self.action.skipped
        if self.read_back and not verified:
            raise ValueError("a True read_back needs a verified or already-wanted delivery")

    @property
    def delivery(self) -> Delivery:
        return self.action.delivery


@dataclass(frozen=True)
class Ports:
    decide: Callable[[TurnView], Decision]
    observe: Callable[[], Observed]
    act: Callable[[Dispatch], ActResult]
    """Send one action and read it back where the verb has a read-back ([V2.24] item 3)."""
    watch: Callable[[Observed, Dispatch], Watched]
    write_ahead: Callable[[Dispatch], str]
    interrupted: Callable[[], str | None]
    """A takeover, the secure desktop or a lock, as a token; None to go on."""
    clock: Callable[[], float]


@dataclass(frozen=True)
class TaskOutcome:
    status: Status
    cause: Reason | None
    """Why the task ended, as a pair ([V2.13] decision 5); `reason` is its one rendering."""
    dispatched: tuple[Dispatch, ...]
    model_calls: int
    strikes: tuple[tuple[StrikeReason, ...], ...]
    """The evidence: one entry per strike, with every reason that held in that turn."""

    @property
    def reason(self) -> str | None:
        """The closed token: `commit_not_in_step`, `no_progress`, `after_refusal:<token>`..."""
        return render_reason(self.cause)


def resolve(target: int, listed: Snapshot, latest: Snapshot) -> Element | None:
    """The element `target` named in `listed`, if the latest snapshot still has its RuntimeId
    with the same role and name (§8.1); None when it is gone, changed, or has no RuntimeId.
    v1's `resolve_in` decides it; another window or an older `latest` is misuse and raises."""
    try:
        return resolve_in(listed, target, latest)
    except SteerError as exc:
        if exc.code is ErrorCode.ELEMENT_NOT_FOUND:
            return None
        raise


@dataclass(frozen=True)
class PendingText:
    """[V2.22] a field Steer typed into under the action's subgoal, with the digest of what it
    typed and the field's live value now; the text is pending while `now` still holds it."""

    field: tuple[int, ...]
    typed: ValueFact
    now: ValueFact | None
    """The live read of the field now; None when it is gone or unread."""
    search: SearchFacts
    goal_span: bool
    """The typed text was a goal span (by param)."""


@stub
def action_risk(  # noqa: PLR0913 - the action, its target and keyword-only facts
    action: ActionDoc,
    element: Element,
    *,
    goal: str,
    search: SearchFacts | None,
    observed: Observed | None = None,
    pending: Sequence[PendingText] = (),
) -> Risk:
    """§7.2: the max of the declared commit, deny words, a secure field and text provenance;
    raise-only, so no model field lowers it (§8.2). `search` is the field's facts for an Enter
    or a click that may be a query.

    [V2.22] the submit-button hole: a click is a submit of a field whose text is still pending
    (`pending`, read on `observed`) when the target sits under that field's parent container or
    shares its nearest landmark and is not itself a field or a state-set role (Edit, ComboBox,
    CheckBox, RadioButton, ListItem, TabItem, TreeItem). It is then classified like the Enter:
    R1 query when every §7.2 condition holds, else R2. Once the field is emptied or gone the
    click is a plain click."""
    raise NotImplementedError("3c: action risk")


@stub
def run_task(task: Task, ports: Ports) -> TaskOutcome:
    """Run `task` to an outcome. Between batch actions the watch's after snapshot is reused only
    when it is a full-tier read, stable and within v1's R0/R1 freshness limit
    (`REUSE_FRESHNESS_S`, its age `clock() - snapshot.taken_at`); otherwise the loop
    re-observes ([V2.11] decision 6)."""
    raise NotImplementedError("3c: the v2 loop")
