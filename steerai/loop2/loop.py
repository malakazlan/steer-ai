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
from dataclasses import field as dataclass_field
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
ACTIVATING_VERBS: Final = frozenset(
    {"click", "focus", "expand", "collapse", "check", "uncheck", "select"}
)
"""Every verb that can activate an element: the [V2.22] submit rule applies to each (principal
review 2); the role exclusion keeps real state targets out."""
SUBMIT_KEYS: Final = frozenset({"Enter", "Space"})
"""Keys that activate a non-field element the way a click does ([V2.22])."""
NOT_A_SUBMIT: Final = FIELD_ROLES | frozenset(
    {"CheckBox", "RadioButton", "ListItem", "TabItem", "TreeItem"}
)
"""[V2.22] addendum: a field or a state-set role is never a submit of a field's text."""
SENT_COMMITS: Final = frozenset({"send", "submit"})
NO_SEARCH = SearchFacts(
    searchbox=False, in_search_landmark=False, secure_in_form=False, payment_in_form=False
)
_ORDER: Final = {risk: index for index, risk in enumerate(RISKS)}
AskReason = Literal["generated_text", "text_changed", "submit_pending_text"]
"""[V2.25]: `generated_text` and `text_changed` are asks no grant covers; `submit_pending_text`
marks a tier the submit rule raised, so its blocks are counted on their own."""
ParentKind = Literal["document", "main", "narrower"]
Held = Literal["held", "changed", "unread"]
"""A typed field still in the snapshot and not empty: what Steer typed (same digest), something
else (a reformat or an autocomplete), or a value that could not be read."""


@dataclass(frozen=True)
class RiskVerdict:
    """What `action_risk` decides: the tier, the ask reasons beside it, the kind of the field's
    parent when the submit rule fired (for the per-kind count), and a refusal: a declared send
    or submit of text that changed since Steer typed it is refused (`text_changed`), so the
    controller clears and retypes ([V2.25] addendum)."""

    risk: Risk
    reasons: frozenset[AskReason] = frozenset()
    submit_parent: ParentKind | None = None
    refusal: Literal["text_changed"] | None = None


@dataclass
class _Tally:
    risk: Risk = "R0"
    reasons: set[AskReason] = dataclass_field(default_factory=set)
    submit_parent: ParentKind | None = None
    refused: bool = False

    def raise_to(self, tier: Risk) -> None:
        self.risk = _highest(self.risk, tier)


def _highest(*tiers: Risk) -> Risk:
    return max(tiers, key=_ORDER.__getitem__)


def action_risk(
    action: ActionDoc,
    element: Element,
    *,
    goal: str,
    observed: Observed,
    pending: Sequence[PendingText] = (),
) -> RiskVerdict:
    """§7.2, §8.2: raise-only, so no model field lowers it. The tier is the max of:

    - the declared commit (`COMMIT_RISK`) and v1's R1 floor for the state verbs and `choose`;
    - v1's deny, control, consent and billing patterns on `element` (`gate.deny_tier_of`), with
      its group name read on `observed`. `element` is the element the action goes to: for a
      press, the last acted element, so a press takes that element's deny scoping. A scroll and
      a wait read no deny words;
    - a `type` into a secure field: R3 (the loop also refuses it);
    - an Enter on a field: `query.classify` with the field's own pending text (its search facts
      and goal span); no pending text, an unread value or a changed one is never a query;
    - [V2.22] the submit rule, for every activating verb and an Enter or Space on a non-field:
      a target that is not a field or a state-set role, while a field typed under this subgoal
      is non-empty, sitting under that field's parent container or sharing its nearest
      landmark, is classified like the Enter. No Document or Main exception ([V2.25] (b)). A
      missing fact (an unread value or landmark map, a broken parent chain) fails closed to R2.

    Reasons ([V2.25]): `generated_text` on any action at R2 or above that types free text or
    while a field holds generated text; `text_changed` on a caution submit of text that changed
    since Steer typed it (a declared send or submit of it is refused instead);
    `submit_pending_text` when the submit rule raised the tier. Below R2 nothing is added: free
    text stays at its tier and the dispatch's provenance marks it in the trace.

    An empty name raises nothing by itself; an unnamed target is still raised by its automation
    id, its group name and the submit rule. `goal` is kept for the step-4 goal check."""
    del goal  # the per-action goal check is step 4
    tally = _Tally(COMMIT_RISK[action.commit])
    tally.raise_to("R1" if action.verb in STATE_FLOOR else "R0")
    deny: Risk = "R0"
    if action.verb not in NO_DENY_VERBS:
        deny = deny_tier_of(_deny_facts(action, element, observed))
        tally.raise_to(deny)
        if _parent_unread(element, observed):
            tally.raise_to("R2")  # its group name cannot be read: never read as "no group"
    if action.verb == "type" and element.secure:
        tally.raise_to("R3")
    states = _states(observed, pending)
    denied = _ORDER[deny] >= _ORDER["R2"]
    is_key = action.verb == "press"
    if is_key and action.key == "Enter" and element.role in FIELD_ROLES:
        _enter(tally, action, element, states, denied=denied)
    elif action.verb in ACTIVATING_VERBS or (is_key and action.key in SUBMIT_KEYS):
        _submit(tally, action, element, observed, states, denied=denied)
    if _ORDER[tally.risk] >= _ORDER["R2"] and _generated(action, states):
        tally.reasons.add("generated_text")
    return RiskVerdict(
        tally.risk,
        frozenset(tally.reasons),
        tally.submit_parent,
        "text_changed" if tally.refused else None,
    )


def _parent_unread(element: Element, observed: Observed) -> bool:
    """The element names a parent the snapshot does not hold, so `gate.group_name` would read
    "no group" for a fact that was simply not observed."""
    return element.parent_id is not None and all(
        e.id != element.parent_id for e in observed.snapshot.elements
    )


def _deny_facts(action: ActionDoc, element: Element, observed: Observed) -> DenyFacts:
    return DenyFacts(
        verb=action.verb,
        role=element.role,
        name=element.name,
        automation_id=element.automation_id,
        option=action.option,
        group_name=group_name(observed.snapshot, element),
    )


def _states(observed: Observed, pending: Sequence[PendingText]) -> list[tuple[PendingText, Held]]:
    """The typed fields the submit rule is on for: still in the snapshot (else gone) and not
    emptied ([V2.25] addendum: a changed digest keeps it on)."""
    shown = {e.runtime_id for e in observed.snapshot.elements}
    states: list[tuple[PendingText, Held]] = []
    for held in pending:
        if held.field not in shown:
            continue
        if held.now is None:
            states.append((held, "unread"))
        elif held.now.filled:
            same = held.now.digest == held.typed.digest
            states.append((held, "held" if same else "changed"))
    return states


def _generated(action: ActionDoc, states: Sequence[tuple[PendingText, Held]]) -> bool:
    free_text = action.verb == "type" and action.param is None
    return free_text or any(not held.goal_span for held, _ in states)


def _classified(  # noqa: PLR0913 - one submit: the tally, the action, its text and facts
    tally: _Tally,
    action: ActionDoc,
    held: PendingText,
    state: Held,
    *,
    verb: Literal["press", "click"],
    denied: bool,
    same_landmark: bool = False,
) -> Risk:
    """One submit of `held`'s text, like the Enter. Unread fails closed; a changed text is never
    a query, asks `text_changed` under a commit-none subgoal, and refuses a declared send."""
    if state == "unread":
        return "R2"
    if state == "changed":
        if action.commit in SENT_COMMITS:
            tally.refused = True
        elif action.commit == "none":
            tally.reasons.add("text_changed")
    return classify(
        verb=verb,
        field=held.search,
        goal_span=held.goal_span and state == "held",
        commit=action.commit,
        deny_word=denied,
        same_landmark=same_landmark,
    )


def _enter(
    tally: _Tally,
    action: ActionDoc,
    field: Element,
    states: Sequence[tuple[PendingText, Held]],
    *,
    denied: bool,
) -> None:
    mine = next(((h, s) for h, s in states if h.field == field.runtime_id), None)
    if mine is None:
        tally.raise_to(
            classify(
                verb="press",
                field=NO_SEARCH,
                goal_span=False,
                commit=action.commit,
                deny_word=denied,
            )
        )
        return
    held, state = mine
    tally.raise_to(_classified(tally, action, held, state, verb="press", denied=denied))


def _submit(  # noqa: PLR0913 - the tally, the action, its target and the read facts
    tally: _Tally,
    action: ActionDoc,
    target: Element,
    observed: Observed,
    states: Sequence[tuple[PendingText, Held]],
    *,
    denied: bool,
) -> None:
    if not states or target.role in NOT_A_SUBMIT:
        return
    for held, state in states:
        beside = _beside(observed, held.field, target)
        if beside is False:
            continue
        tally.submit_parent = tally.submit_parent or _parent_kind(observed, held.field)
        if beside is None:
            tier: Risk = "R2"
        else:
            same = shares_search_landmark(observed, held.field, target.runtime_id)
            tier = _classified(
                tally, action, held, state, verb="click", denied=denied, same_landmark=same
            )
        if _ORDER[tier] >= _ORDER["R2"]:
            tally.reasons.add("submit_pending_text")
        tally.raise_to(tier)


def _beside(observed: Observed, field: tuple[int, ...], target: Element) -> bool | None:
    """[V2.22] the target is under the field's parent container, or shares its nearest landmark;
    None when a fact needed to tell is missing (a broken parent chain, or the landmark map or
    either element in it not read)."""
    by_id = {e.id: e for e in observed.snapshot.elements}
    typed = next(e for e in observed.snapshot.elements if e.runtime_id == field)
    if typed.parent_id is not None:
        under = _under(target, typed.parent_id, by_id)
        if under is not False:
            return under
    marks = observed.landmarks
    if marks is None or field not in marks or target.runtime_id not in marks:
        return None
    mine, theirs = marks[field], marks[target.runtime_id]
    return mine is not None and theirs is not None and mine.runtime_id == theirs.runtime_id


def _under(element: Element, ancestor: int, by_id: Mapping[int, Element]) -> bool | None:
    """True when `ancestor` is above `element`; None when the chain breaks (a parent id that is
    not in the snapshot): unknown, never "elsewhere" (principal review 3)."""
    seen: set[int] = set()
    parent = element.parent_id
    while parent is not None and parent not in seen:
        if parent == ancestor:
            return True
        current = by_id.get(parent)
        if current is None:
            return None
        seen.add(parent)
        parent = current.parent_id
    return False


def _parent_kind(observed: Observed, field: tuple[int, ...]) -> ParentKind:
    """[V2.25] (b): the field's parent is the Document, a Main landmark, or narrower."""
    by_id = {e.id: e for e in observed.snapshot.elements}
    typed = next(e for e in observed.snapshot.elements if e.runtime_id == field)
    parent = None if typed.parent_id is None else by_id.get(typed.parent_id)
    if parent is None or parent.role == "Document":
        return "document"
    nearest = None if observed.landmarks is None else observed.landmarks.get(field)
    if nearest is not None and nearest.kind == "main" and nearest.runtime_id == parent.runtime_id:
        return "main"
    return "narrower"


@stub
def run_task(task: Task, ports: Ports) -> TaskOutcome:
    """Run `task` to an outcome. Between batch actions the watch's after snapshot is reused only
    when it is a full-tier read, stable and within v1's R0/R1 freshness limit
    (`REUSE_FRESHNESS_S`, its age `clock() - snapshot.taken_at`); otherwise the loop
    re-observes ([V2.11] decision 6)."""
    raise NotImplementedError("3c: the v2 loop")
