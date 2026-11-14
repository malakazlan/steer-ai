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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from steerai.action.dispositions import ActionResult, Delivery
from steerai.errors import ErrorCode, SteerError
from steerai.loop2._stub import stub
from steerai.loop2.decision import ActionDoc, Decision
from steerai.loop2.guards import StrikeReason
from steerai.loop2.query import SearchFacts, classify, shares_search_landmark
from steerai.loop2.task import Commit, Task
from steerai.orchestrator.contracts import RISKS, Risk
from steerai.orchestrator.gate import DenyFacts, deny_tier_of, group_name
from steerai.verify.predicates import FIELD_ROLES
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


COMMIT_RISK: Final[Mapping[Commit, Risk]] = {
    "none": "R0",
    "send": "R2",
    "submit": "R2",
    "delete": "R3",
    "pay": "R3",
}
"""A declared commit's tier: v1's verb floors (send and submit R2, delete R3) and its deny list
(`pay` R3)."""
STATE_FLOOR: Final = frozenset({"check", "uncheck", "select", "choose", "expand", "collapse"})
"""v1's R1 floor for the state verbs and `choose` (contracts `_RISK_FLOOR`)."""
NO_DENY_VERBS: Final = frozenset({"scroll", "wait"})
"""A scroll moves the view and a wait sends nothing: no deny word on them raises the tier; a
declared commit still does."""
SUBMIT_KEYS: Final = frozenset({"Enter", "Space"})
"""Keys that activate a non-field element the way a click does ([V2.22])."""
NOT_A_SUBMIT: Final = FIELD_ROLES | frozenset(
    {"CheckBox", "RadioButton", "ListItem", "TabItem", "TreeItem"}
)
"""[V2.22] addendum: a field or a state-set role is never a submit of a field's text."""
NO_SEARCH = SearchFacts(
    searchbox=False, in_search_landmark=False, secure_in_form=False, payment_in_form=False
)
_ORDER: Final = {risk: index for index, risk in enumerate(RISKS)}


def _highest(*tiers: Risk) -> Risk:
    return max(tiers, key=_ORDER.__getitem__)


def action_risk(  # noqa: PLR0913 - the action, its target and keyword-only facts
    action: ActionDoc,
    element: Element,
    *,
    goal: str,
    search: SearchFacts | None,
    observed: Observed | None = None,
    pending: Sequence[PendingText] = (),
) -> Risk:
    """§7.2, §8.2: raise-only, so no model field lowers it. The max of:

    - the declared commit (`COMMIT_RISK`) and v1's R1 floor for the state verbs and `choose`;
    - v1's deny, control, consent and billing patterns on `element` (`gate.deny_tier_of`), with
      its group name read on `observed`. `element` is the element the action goes to: for a
      press, the last acted element, so a press takes that element's deny scoping. A scroll and
      a wait read no deny words;
    - a `type` into a secure field: R3 (the loop also refuses it);
    - an Enter on a field: `query.classify`, with `search` the field's facts (None fails closed
      to a commit) and the goal span from the field's pending text;
    - [V2.22] the submit rule: a click, or an Enter or Space on a non-field, on a target that is
      not a field or a state-set role, while a field typed under this subgoal still holds its
      digest (`pending`), when the target sits under that field's parent container or shares
      its nearest landmark: classified like the Enter. A missing fact fails closed (R2);
    - text provenance: free text (not a param) at R2 or above is raised to R3, never covered by
      an envelope grant (§8.5). Below R2 it does not change the tier.

    An empty name raises nothing by itself; an unnamed target is still raised by its automation
    id, its group name and the submit rule. `goal` is kept for the step-4 goal check."""
    del goal  # the per-action goal check is step 4
    tier = _highest(COMMIT_RISK[action.commit], "R1" if action.verb in STATE_FLOOR else "R0")
    deny: Risk = "R0"
    if action.verb not in NO_DENY_VERBS:
        deny = deny_tier_of(_deny_facts(action, element, observed))
        tier = _highest(tier, deny)
    if action.verb == "type" and element.secure:
        tier = "R3"
    denied = _ORDER[deny] >= _ORDER["R2"]
    if action.verb == "press" and action.key == "Enter" and element.role in FIELD_ROLES:
        tier = _highest(tier, _enter_tier(action, element, search, pending, denied=denied))
    elif action.verb == "click" or (action.verb == "press" and action.key in SUBMIT_KEYS):
        tier = _highest(tier, _submit_tier(action, element, observed, pending, denied=denied))
    if action.verb == "type" and action.param is None and _ORDER[tier] >= _ORDER["R2"]:
        tier = "R3"
    return tier


def _deny_facts(action: ActionDoc, element: Element, observed: Observed | None) -> DenyFacts:
    group = None if observed is None else group_name(observed.snapshot, element)
    return DenyFacts(
        verb=action.verb,
        role=element.role,
        name=element.name,
        automation_id=element.automation_id,
        option=action.option,
        group_name=group,
    )


def _holding(pending: Sequence[PendingText]) -> list[PendingText]:
    """The typed fields that still hold exactly what Steer typed (same digest, filled)."""
    return [
        p for p in pending if p.now is not None and p.now.filled and p.now.digest == p.typed.digest
    ]


def _enter_tier(
    action: ActionDoc,
    field: Element,
    search: SearchFacts | None,
    pending: Sequence[PendingText],
    *,
    denied: bool,
) -> Risk:
    held = next((p for p in _holding(pending) if p.field == field.runtime_id), None)
    return classify(
        verb="press",
        field=search if search is not None else NO_SEARCH,
        goal_span=held is not None and held.goal_span,
        commit=action.commit,
        deny_word=denied,
    )


def _submit_tier(
    action: ActionDoc,
    target: Element,
    observed: Observed | None,
    pending: Sequence[PendingText],
    *,
    denied: bool,
) -> Risk:
    live = _holding(pending)
    if not live or target.role in NOT_A_SUBMIT:
        return "R0"
    if observed is None:
        return "R2"
    tier: Risk = "R0"
    for held in live:
        beside = _beside(observed, held.field, target)
        if beside is None:
            return "R2"
        if beside:
            same = shares_search_landmark(observed, held.field, target.runtime_id)
            classified = classify(
                verb="click",
                field=held.search,
                goal_span=held.goal_span,
                commit=action.commit,
                deny_word=denied,
                same_landmark=same,
            )
            tier = _highest(tier, classified)
    return tier


def _beside(observed: Observed, field: tuple[int, ...], target: Element) -> bool | None:
    """[V2.22] the target is under the field's parent container, or shares its nearest landmark;
    None when a fact needed to tell is missing (the field is not in the snapshot, or the
    landmark map or either element in it was not read)."""
    by_id = {e.id: e for e in observed.snapshot.elements}
    typed = next((e for e in observed.snapshot.elements if e.runtime_id == field), None)
    if typed is None:
        return None
    if typed.parent_id is not None and _under(target, typed.parent_id, by_id):
        return True
    marks = observed.landmarks
    if marks is None or field not in marks or target.runtime_id not in marks:
        return None
    mine, theirs = marks[field], marks[target.runtime_id]
    return mine is not None and theirs is not None and mine.runtime_id == theirs.runtime_id


def _under(element: Element, ancestor: int, by_id: Mapping[int, Element]) -> bool:
    seen: set[int] = set()
    current = None if element.parent_id is None else by_id.get(element.parent_id)
    while current is not None and current.id not in seen:
        if current.id == ancestor:
            return True
        seen.add(current.id)
        current = None if current.parent_id is None else by_id.get(current.parent_id)
    return False


@stub
def run_task(task: Task, ports: Ports) -> TaskOutcome:
    """Run `task` to an outcome. Between batch actions the watch's after snapshot is reused only
    when it is a full-tier read, stable and within v1's R0/R1 freshness limit
    (`REUSE_FRESHNESS_S`, its age `clock() - snapshot.taken_at`); otherwise the loop
    re-observes ([V2.11] decision 6)."""
    raise NotImplementedError("3c: the v2 loop")
