"""The v2 loop's shapes for step 3 (V2 §5), behind `--loop v2`: ports, the resolver and the
action risk. `run_task` itself is in `steerai.loop2.runner`. Every outside effect goes through
`Ports`, so the tests drive the loop with fakes.

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

import dataclasses
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Final, Literal, get_args

from steerai.action.dispositions import ActionResult, Delivery
from steerai.errors import ErrorCode, SteerError
from steerai.loop2.decision import ActionDoc, Decision
from steerai.loop2.form import (
    ACTING_ROLES,
    ENTER_FIELDS,
    STATE_SET_VERB,
    beside,
    form_facts,
)
from steerai.loop2.guards import StrikeReason
from steerai.loop2.history import RECORD_TYPES, HistoryRecord, RefusalWord, render
from steerai.loop2.listing import ListingTurn
from steerai.loop2.query import SearchFacts, classify, search_landmark_of
from steerai.loop2.task import Commit, Subgoal
from steerai.orchestrator.contracts import RISKS, Risk
from steerai.orchestrator.gate import DenyFacts, deny_tier_of, group_name
from steerai.verify.predicates import FIELD_ROLES
from steerai.world.effects import Observed, ValueFact
from steerai.world.elements import Element
from steerai.world.model import Snapshot, resolve_in
from steerai.world.reads import ReadCost
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
    role: str
    name: str
    """The resolved element's role and name, so the act port re-checks them on the live
    element at dispatch (§8.1, RT-04); empty for a wait."""
    risk: Risk
    provenance: Provenance | None
    """Set by code, never by the model, for a `type` ([V2.11] decision 5): `param` gives
    `goal_span`; free text is always `generated`, even when it matches the request."""

    submit: bool = False
    """[V2.28] item 1: the action goes through the Enter table (an Enter on a field, or a
    submit of pending text), so the watch holds it to a conclusive report; set by code."""
    trigger: bool = False
    """[V2.50]: the risk path classed it an activation with no read-back (`RiskVerdict.trigger`),
    so the watch holds it as a trigger; set by code."""
    own_text: bool = False
    """[V2.51] (5): a Backspace goes only to a field holding Steer's own pending text; the
    runner's press rule sets it from its pending-text records, and the act port refuses a
    Backspace without it (the second layer). Set by code."""
    text: str | None = dataclasses.field(default=None, repr=False)
    """RT-36: the text a `type` sends, resolved by code (the param's goal span, or the free
    text); the act port types it. Never in a repr, a trace or the outcome."""

    def redacted(self) -> Dispatch:
        """The dispatch as the outcome keeps it (RT-18): no typed text, option or name."""
        action = self.action.model_copy(update={"text": None, "option": None})
        return dataclasses.replace(self, action=action, name="", text=None)


@dataclass(frozen=True)
class TurnView:
    """What one controller call is given: the latest observation and closed history tokens."""

    observed: Observed
    log: tuple[HistoryRecord, ...]
    """[V2.46], [V2.49]: what happened so far, in order, as typed records (closed tokens, the
    task's ids and issued handles). Never screen text or model text (§8.6), never a string."""
    first_turn: bool
    handles: Mapping[int, int]
    """[V2.38] RT-17: each listed element's task handle, by its snapshot id. The listing shows
    these, and the model names elements by them; an element with no RuntimeId has none."""
    subgoals: tuple[Subgoal, ...] = ()
    """The task's subgoals as frozen at intake (none before the first Decision)."""
    proven: frozenset[str] = frozenset()
    """The subgoal ids proven so far: their status is `done`."""

    issued: frozenset[int] = frozenset()
    """[V2.49]: every task handle issued so far: the handles a history record may name."""
    keep: frozenset[tuple[int, ...]] = frozenset()
    """[V2.54] always-keep, by RuntimeId: every element acted on in this task and every field
    holding pending text. The listing shows them before anything else while they exist."""
    new: frozenset[tuple[int, ...]] = frozenset()
    changed: frozenset[tuple[int, ...]] = frozenset()
    """[V2.56]: what appeared, and what changed, since the observation the model last saw."""
    changes: Literal["compared", "no_comparison", "states_unread"] = "no_comparison"
    """[V2.56]: whether this turn had a comparison, and whether its states were read."""

    def __post_init__(self) -> None:
        if not all(isinstance(r, RECORD_TYPES) for r in self.log):
            raise TypeError("history entries are records, never strings ([V2.49])")

    @property
    def history(self) -> tuple[str, ...]:
        """The log as its lines (§8.6), each by `history.render`."""
        return tuple(render(r) for r in self.log)


READ_BACK_VERBS = frozenset(
    {"type", "choose", "focus", "check", "uncheck", "select", "expand", "collapse"}
)
"""The verbs whose actuation reads its result back ([V2.11] item 2, [V2.5]): type, choose, the
state verbs, and focus (the live keyboard focus, [V2.51] (1))."""


@dataclass(frozen=True)
class ActResult:
    """[V2.24] item 3: what one dispatch returns to the loop. `action` is v1's `ActionResult`
    (its delivery, its code, and the method steps, label and mechanism, kept for step 5);
    `read_back` is the three-valued read-back of a read-back verb: True verified, False a
    mismatch, None unread. The read-back belongs to the actuation (§7.3: type, then read back),
    so it travels here and no separate port reads it. [V2.51] (2): both facts are kept as they
    are; v2's "verified" is the read-back alone (a v1 `delivered_verified` with a False read-back
    is a mismatch, and a True read-back stands after any delivery)."""

    action: ActionResult
    read_back: bool | None

    def __post_init__(self) -> None:
        if self.read_back is None:
            return
        if self.action.verb not in READ_BACK_VERBS:
            raise ValueError("read_back is only for type, choose, focus and the state verbs")
        sent_nothing = self.action.delivery is Delivery.NOT_DELIVERED and not self.action.skipped
        if sent_nothing:
            raise ValueError("read_back needs a delivery: nothing was sent")

    @property
    def delivery(self) -> Delivery:
        return self.action.delivery


@dataclass(frozen=True)
class TokenUsage:
    """[V2.37] P6, [V2.40] Q3, [V2.45]: the tokens one controller call used, as the provider
    reported them, by category. `input_tokens` excludes the cache categories; the two writes split
    the API's `cache_creation_input_tokens` by lifetime (its `cache_creation` breakdown), and
    `cache_read` is its `cache_read_input_tokens`."""

    input_tokens: int
    output_tokens: int
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0


class PortInterrupted(Exception):  # noqa: N818 - the packet's name ([V2.40] Q4)
    """A port abandoned its wait because `interrupted()` reported a token, or because its result
    belongs to a task generation that is no longer current ([V2.40] Q4, [V2.41] (1)). It carries
    no authority: the runner re-reads `interrupted()`, and only the latched switch stops the task
    as killed."""

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


UrlRead = Literal["read", "no_bar", "document_url_unread", "not_composed"]
"""Stage 2 plan §5: why an observation's URL is what it is, in closed tokens."""
URL_READS: Final[tuple[UrlRead, ...]] = get_args(UrlRead)


def url_read(observed: Observed) -> UrlRead:
    """`read`; `no_bar` (the window has no address bar observed); `document_url_unread` (the top
    Document's URL was not read); `not_composed` (it was, but the address bar did not confirm it:
    the bar unread, or a host the two do not agree on)."""
    if observed.url is not None:
        return "read"
    if not observed.url_observed:
        return "no_bar"
    if observed.document_url is None:
        return "document_url_unread"
    return "not_composed"


class WindowCovered(Exception):  # noqa: N818 - a control-flow signal, as PortInterrupted
    """[V2.62] the bound window is covered (`world.cover`: minimised, cloaked, or wholly under
    the windows above it), read before any wait for a page: the task ends at once `stopped` /
    `window_covered`, never `page_not_ready` after the deadline, and Steer never raises the
    window itself. `token` is the cover's own reason, for the trace."""

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


@dataclass(frozen=True)
class Decided:
    """What the decide port returns: the Decision and the call's usage; None when the usage was
    not reported, which trips the token budget."""

    decision: Decision
    usage: TokenUsage | None
    listing: ListingTurn | None = None
    """[V2.54]: the listing shown for this Decision; None: every bound handle was shown."""
    exact_counted: bool = False
    """[V2.61]: the prompt guard spent an exact count on this turn."""
    estimate_low: bool = False
    """[V2.61]: the turn's calls held more input tokens than the guard's bound said."""
    calls: int = 1
    """Invariant 9, [V2.68]: the model calls the turn made (a repaired turn is two)."""


AuditReason = Literal["outcome_shown", "outcome_not_shown", "outcome_unclear"]
"""[V2.64] One: what the final audit says of the request's outcome (closed)."""
ProofLine = tuple[str, str, str, str, bool]
"""One proven subgoal for the audit: (subgoal id, closes, how, kind, closed only by value or
state claims)."""


@dataclass(frozen=True)
class AuditView:
    """[V2.64] One, rev 4.5: what the final audit sees: the final observation (its listing), the
    task handles bound on it, and the proof tokens. The request comes from the task."""

    observed: Observed
    handles: Mapping[int, int]
    proofs: tuple[ProofLine, ...]
    keep: frozenset[tuple[int, ...]] = frozenset()
    """#404 principal 2: decide's always-keep set plus every cited element, by RuntimeId: the
    audit's listing is selected as decide's is, with these kept under the cap."""
    words: frozenset[str] = frozenset()
    """The subgoals' param words, for the goal-word class of the listing."""


@dataclass(frozen=True)
class Audited:
    """The audit's answer: a veto or not, its closed reason, and the call's usage."""

    veto: bool
    reason: AuditReason
    usage: TokenUsage | None = None


@dataclass(frozen=True)
class Ports:
    decide: Callable[[TurnView], Decided]
    """One controller call, with the tokens it used ([V2.37] P6)."""
    observe: Callable[[], Observed]
    """The window's observation. The adapter's first call per process window goes through
    `observe2.first_observation`, so "the first observation already waited out the first-ready
    deadline" holds only on that path ([V2.31], [V2.44] (2)); `FirstTouches.ready` hands over an
    already-touched process on its first read, with no wait."""
    act: Callable[[Dispatch], ActResult]
    """Send one action and read it back where the verb has a read-back ([V2.24] item 3)."""
    watch: Callable[[Observed, Dispatch], Watched]
    write_ahead: Callable[[Dispatch], str]
    """The R2/R3 record before dispatch (closed in step 3). The Dispatch carries the typed text
    (RT-36): a record written to disk takes `Dispatch.redacted()` or a digest, never the text."""
    interrupted: Callable[[], str | None]
    """A takeover, the secure desktop or a lock, as a token; None to go on."""
    clock: Callable[[], float]
    """The loop's clock, in the domain of `Snapshot.taken_at` (a read's completion): the reuse
    between batch actions compares the two ([V2.11] decision 6, [V2.34])."""
    stamp: Callable[[Observed], Observed]
    """[V2.31], RT-10/RT-11: the adapter's one `observe2.DocumentSince`, on `clock`: the loop
    passes every observation it uses through it (the watch's after and the settle result
    included), so `document_since` and `young` come from one stamp in one clock domain."""
    settle: Callable[[Observed, float], Observed]
    """[V2.31] from this observation, wait until the page's node count holds for the loop's quiet
    window or the guard (seconds) expires, and return the last read (`observe2.hold_still`)."""
    read_value: Callable[[Element], ValueFact | None]
    """[V2.28] item 3, [V2.29] Q2: one element's value fact, read from the element itself through
    its live handle with the role and name re-check; None when unread. Never called on a field
    whose secure flag is set or unread."""
    read_state: Callable[[Element, str], bool | None]
    """[V2.29] Q2: whether the element is in the wanted state (on, off, selected, expanded,
    collapsed), read live through its handle with the role and name re-check; None when unread."""
    fact_of: Callable[[str], ValueFact]
    """The value fact a field holding exactly this text reads as (the session key and the
    normalisation `read_value` uses), so a value claim compares digests, never text."""
    audit: Callable[[AuditView], Audited] | None = None
    """[V2.64] One: the final audit, called only when every proof is self-written; None means
    there is none, so such a task fails closed (`audit_failed`), never ends done unaudited."""


WatchKind = Literal["plain", "trigger", "submit", "wait"]
"""[V2.48]: the watch classes, each with its own deadline (`world_ports.WorldTimes`)."""


def watch_kind(dispatch: Dispatch) -> WatchKind:
    """How the watch after a dispatch holds (§7.5, [V2.8], [V2.28] 1, [V2.41], [V2.48], [V2.50]).
    It reads only what the risk path decided and put on the dispatch, never the verb or the key
    again, so the timing and the risk classes cannot disagree:
    - `submit`: the risk path routed it through the Enter table (`Dispatch.submit`);
    - `wait`: a wait (it sends nothing), held like a trigger to `WAIT_S`;
    - `trigger`: the risk path classed it an activation with no read-back (`Dispatch.trigger`:
      a click on a non-field, an Enter or a Space that activates an acting element);
    - `plain`: everything else (the read-back verbs, focus, a press on a state-set element)."""
    if dispatch.submit:
        return "submit"
    if dispatch.action.verb == "wait":
        return "wait"
    return "trigger" if dispatch.trigger else "plain"


@dataclass(frozen=True)
class TaskOutcome:
    status: Status
    cause: Reason | None
    """Why the task ended, as a pair ([V2.13] decision 5); `reason` is its one rendering."""
    dispatched: tuple[Dispatch, ...]
    model_calls: int
    """Controller decisions asked for (one per `Ports.decide`, an abandoned or failed one
    included), not provider calls: a decide's repair ask is a second provider call inside it."""
    strikes: tuple[tuple[StrikeReason, ...], ...]
    """The evidence: one entry per strike, with every reason that held in that turn."""
    read_costs: tuple[ReadCost, ...] = ()
    """[V2.34] the reads the loop paid for (count and ms by tier), in order: each post-decision
    observe that reported its cost, and each watched action's watch. The observe layer's share
    of the wall-time split."""
    tokens: float = 0.0
    """Every reported token of every call, all categories ([V2.45]); NaN once a call's usage
    was missing or invalid."""
    stale_observations: int = 0
    """[V2.28] item 1: Decisions discarded because the page moved under them (counted)."""
    target_changes: int = 0
    """[V2.43] (2): Decisions discarded because their target re-rendered after the observation
    the model saw (counted apart from page-level staleness)."""
    focus_losses: int = 0
    """[V2.43] (2): Decisions discarded because focus left a press's target after the listing
    showed it there (counted on its own)."""
    unread_automation_ids: int = 0
    """[V2.26] (2), V2 §6.1, #301: the elements whose automation id was not read, summed over
    every observation the loop took in; counted on the pooled set, the decision stays open."""
    listings: tuple[ListingTurn, ...] = ()
    """[V2.54]: each turn's listing counts (eligible, shown, cut, the class ceilings, the prompt
    guard's cut and the prompt's tokens), for the stage 1 report."""
    focus_moved_after_press: int = 0
    """[V2.43] (1), #298: presses (Space or Enter on a state-set element) after which the watch's
    after-read showed the focus off the target."""
    observations_focus_unread: int = 0
    """[V2.52] (b): observations taken in whose keyboard focus was not read (stage 1 report)."""
    observations_focus_elsewhere: int = 0
    """[V2.52] (b): observations taken in whose focus was on nothing listed (stage 1 report)."""
    type_unfocused: int = 0
    """[V2.51] (3): types refused or re-asked because the keyboard focus was not on the field
    (the evidence for folding the focus click into type, a step 5 ladder question)."""
    second_toggles: int = 0
    """[V2.52] (a): actuations in which a second Toggle fired (the re-read still showed the
    third state)."""
    second_toggles_refused: int = 0
    """[V2.52] (a): actuations whose second Toggle never went: unchanged from the initial
    state, unread, or moved off the third state (`second_toggle_refused`)."""
    toggles_settled_on_reread: int = 0
    """[V2.52] (a): actuations whose re-read before the second Toggle showed the wanted state
    (no second Toggle; a success)."""
    fields_not_empty: int = 0
    """[V2.51] (5): types refused because the field already held text (`field_not_empty`)."""
    not_own_text: int = 0
    """[V2.51] (5): Backspaces refused on a field holding no pending text of Steer's."""
    history_dropped: int = 0
    """[V2.47]: history lines off the history vocabulary (`history_line_ok`): dropped, never
    shown."""
    wall_s: float = 0.0
    """7b: the task's wall time on the ports' clock, start to end (use against `max_wall_s`)."""
    cost_usd: float | None = None
    """Each token category times its configured rate ([V2.40] Q3); None when no price is
    configured or a used category has no rate ([V2.37] P6): the cost budget is then reported
    "not configured", never as enforced, never 0.0."""
    watch_deadlines: tuple[tuple[WatchKind, int, int], ...] = ()
    """[V2.48]: per watch class (every class, in order): the watches and how many ended at their
    deadline, for stage 1's per-class deadline counts. A watch that raised (a kill, a port
    error) never reached a deadline verdict and is in neither count."""
    rejections: tuple[str, ...] = ()
    """The controller's rejection tokens of the turn that ended the task (closed tokens only),
    so a stopped run says why; empty otherwise."""
    usage_incomplete: bool = False
    """[V2.41] (1): some call's usage was missing or invalid: `tokens` and `cost_usd` are then a
    lower bound, never complete. A decide that raises (abandoned on a kill, or failed) is counted
    by the decide port's slice (#326), not here."""
    usage: TokenUsage = TokenUsage(input_tokens=0, output_tokens=0)
    """[V2.45]: the reported usage summed by category (a lower bound when `usage_incomplete`).
    Stage 1 runs with caching off and asserts every cache counter here is zero."""
    exact_counts: int = 0
    """[V2.61]: turns on which the prompt guard spent an exact count (near the ceiling)."""
    audit: tuple[str, str] | None = None
    """[V2.64] One: the final audit's (`allowed` | `vetoed` | `failed`, reason token); None when
    no audit ran (some proof was not self-written, or the task did not reach done)."""
    query_combobox_listed: bool = False
    """[V2.64] Two: a query-class ComboBox (searchbox AriaRole, or under a Search landmark) was
    among the rows the model was shown."""
    typed_into_combobox: bool = False
    """[V2.64] Two: a type targeted a ComboBox (whether it was then sent or refused)."""
    pre_task_draft_clicks: int = 0
    """[V2.64] Two: non-field clicks whose submit scope held a filled field with no pending-text
    record (text Steer did not type: a draft from before the task, or the page's own)."""
    partial_page_calls: int = 0
    """[V2.35]: model calls made on a partial page (the observation growing, or its Document
    younger than the load guard), counted where every call is."""
    refusals: tuple[tuple[RefusalWord, int], ...] = ()
    """[V2.46], [V2.63] (e): every refusal counted by its closed token, in the order first seen
    (the repeat rule is `repeat_send`)."""
    submit_parents: tuple[tuple[ParentKind, int], ...] = ()
    """[V2.25]: the submit rule's firings, counted per kind of the field's parent where each
    action is rated, in `PARENT_KINDS` order."""
    url_reads: tuple[tuple[UrlRead, int], ...] = ()
    """Stage 2 plan §5: every observation taken in, counted by `url_read`, in `URL_READS`
    order."""
    estimates_low: int = 0
    """[V2.61]: turns whose true input tokens beat the guard's bound (each tightened the ratio)."""

    @property
    def reason(self) -> str | None:
        """The closed token: `commit_not_in_step`, `no_progress`, `after_refusal:<token>`..."""
        return render_reason(self.cause)


def type_refusal(
    element: Element, snapshot: Snapshot
) -> Literal["secure_field", "secure_unread"] | None:
    """§8.5, [V2.26] (2): nothing is typed into a secure field, nor into one whose secure flag
    was not read. Either is a refusal with a token and a strike, never an ask: a yes never
    unlocks typing into what may be a password field."""
    if element.secure:
        return "secure_field"
    if element.id in snapshot.unread_secure:
        return "secure_unread"
    return None


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
    typed: ValueFact | None
    """What the field read right after the verified type; None when that read was unread."""
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
"""A scroll moves the view and a wait sends nothing: no deny word on them raises the tier.
[V2.42] RT-42 narrows #297: a declared commit on a wait, a scroll or a focus is now a shape error
refused by `validate` (a model fault), so it never reaches the tier."""
ACTIVATING_VERBS: Final = frozenset(
    {"click", "focus", "expand", "collapse", "check", "uncheck", "select"}
)
"""Every verb that can activate an element: the [V2.22] submit rule applies to each (principal
review 2); the role exclusion keeps real state targets out."""
SUBMIT_KEYS: Final = frozenset({"Enter", "Space"})
"""Keys that activate a non-field element the way a click does ([V2.22])."""
NEIGHBOUR_KEYS: Final = frozenset({"Up", "Down", "Left", "Right", "Home", "End"})
NEIGHBOUR_ROLES: Final = frozenset({"RadioButton", "ListItem", "TabItem"})
"""[V2.26] (1): an arrow, Home or End on these selects a NEIGHBOUR: the press takes the highest
tier among the group's members. The group is the target and its siblings of the same role
under the same parent (the radio group, the list, the tab list). Every ListItem counts as
single-select (multi-select is not observed), which can only raise the tier."""
ActionClass = Literal[
    "enter_field", "activation", "enter_other", "state_press", "field_click", "activating_verb",
    "other",
]  # fmt: skip
TRIGGER_CLASSES: Final[frozenset[ActionClass]] = frozenset({"activation", "enter_other"})
"""[V2.50]: the classes that are an activation with no read-back: their watch is a trigger's."""
_SUBMIT_RULE: Final[frozenset[ActionClass]] = frozenset(
    {"activation", "field_click", "activating_verb"}
)
"""The classes the [V2.22] submit rule applies to (it excludes fields and state-set roles
itself, `NOT_A_SUBMIT`)."""


def action_class(action: ActionDoc, role: str) -> ActionClass:  # noqa: PLR0911 - one per class
    """[V2.50]: how the risk path classes an action by its verb, key and target role; the one
    call that gives both the risk handling (`action_risk`) and the watch timing (a trigger, by
    `TRIGGER_CLASSES`). For a press, `role` is the element it goes to.
    - `enter_field`: an Enter on a field: the Enter table (a submit);
    - `activation`: a click on a non-field, an Enter on an acting role, a Space on a non-field
      that is not state-set: it activates the element, with no read-back (a trigger);
    - `enter_other`: an Enter on anything else: R2 ([V2.26] (3)); an Enter, so a trigger;
    - `state_press`: a Space or an Enter on a state-set role: a state verb with a readable
      state, not a trigger;
    - `field_click`: a click on a field; `activating_verb`: focus and the state verbs (both
      under the submit rule, neither a trigger); `other`: everything else."""
    if action.verb == "press":
        key = action.key
        if key in SUBMIT_KEYS and role in STATE_SET_VERB:
            return "state_press"
        if key == "Enter":
            if role in ENTER_FIELDS:
                return "enter_field"
            return "activation" if role in ACTING_ROLES else "enter_other"
        if key == "Space" and role not in FIELD_ROLES:
            return "activation"
        return "other"
    if action.verb == "click":
        return "field_click" if role in FIELD_ROLES else "activation"
    if action.verb in ACTIVATING_VERBS:
        return "activating_verb"
    return "other"


NOT_A_SUBMIT: Final = FIELD_ROLES | frozenset(
    {"CheckBox", "RadioButton", "ListItem", "TabItem", "TreeItem"}
)
"""[V2.22] addendum: a field or a state-set role is never a submit of a field's text."""
SENT_COMMITS: Final = frozenset({"send", "submit"})
NO_SEARCH = SearchFacts(
    searchbox=False, in_search_landmark=False, secure_in_form=False, risky_in_form=False
)
_ORDER: Final = {risk: index for index, risk in enumerate(RISKS)}
AskReason = Literal["generated_text", "text_changed", "submit_pending_text"]
"""[V2.25]: `generated_text` and `text_changed` are asks no grant covers; `submit_pending_text`
marks a tier the submit rule raised, so its blocks are counted on their own."""
ParentKind = Literal["document", "main", "narrower"]
PARENT_KINDS: Final[tuple[ParentKind, ...]] = get_args(ParentKind)
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
    trigger: bool = False
    """[V2.50]: the action's watch is a trigger's (`action_class` in `TRIGGER_CLASSES`), decided
    by the same call as the risk handling."""
    submit: bool = False
    """#350: the action goes through the Enter table (an Enter on a field, or the submit rule
    fired), decided by the same call; the watch holds it as a submit."""


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
    scoping = _scoping_verb(action, element)
    tally = _Tally(COMMIT_RISK[action.commit])
    tally.raise_to("R1" if scoping in STATE_FLOOR else "R0")
    deny: Risk = "R0"
    if action.verb not in NO_DENY_VERBS:
        deny = _deny(action, element, observed, scoping)
        tally.raise_to(deny)
        if _parent_unread(element, observed):
            tally.raise_to("R2")  # its group name cannot be read: never read as "no group"
    if action.verb == "type" and element.secure:
        tally.raise_to("R3")
    states = _states(observed, pending)
    denied = _ORDER[deny] >= _ORDER["R2"]
    kind = action_class(action, element.role)  # [V2.50]: the one call, risk and timing
    if kind == "enter_field":
        _enter(tally, action, element, observed, states, denied=denied)
    elif kind == "enter_other":
        tally.raise_to("R2")  # [V2.26] (3): an Enter on anything else, or no known target
    elif kind in _SUBMIT_RULE:
        _submit(tally, action, element, observed, states, denied=denied)
    if _ORDER[tally.risk] >= _ORDER["R2"] and _generated(action, states):
        tally.reasons.add("generated_text")
    return RiskVerdict(
        tally.risk,
        frozenset(tally.reasons),
        tally.submit_parent,
        "text_changed" if tally.refused else None,
        trigger=kind in TRIGGER_CLASSES,
        submit=kind == "enter_field" or tally.submit_parent is not None,
    )


def _parent_unread(element: Element, observed: Observed) -> bool:
    """The element names a parent the snapshot does not hold, so `gate.group_name` would read
    "no group" for a fact that was simply not observed."""
    return element.parent_id is not None and all(
        e.id != element.parent_id for e in observed.snapshot.elements
    )


def _scoping_verb(action: ActionDoc, element: Element) -> str:
    """The verb whose deny scoping the action takes ([V2.26]): a Space or an Enter on a
    state-set role takes its state verb, an Enter on an acting role a click's; any other action
    its own verb (a press keeps the element it goes to)."""
    if action.verb != "press":
        return action.verb
    if action.key in {"Space", "Enter"} and element.role in STATE_SET_VERB:
        return STATE_SET_VERB[element.role]
    if action.key == "Enter" and element.role in ACTING_ROLES:
        return "click"
    return action.verb


def _deny(action: ActionDoc, element: Element, observed: Observed, scoping: str) -> Risk:
    """v1's deny tier for the element, or, for an arrow, Home or End on a radio, a list item or
    a tab, the highest among the members of its group ([V2.26] (1))."""
    neighbour = action.verb == "press" and action.key in NEIGHBOUR_KEYS
    if not (neighbour and element.role in NEIGHBOUR_ROLES):
        return deny_tier_of(_deny_facts(action, element, observed, verb=scoping))
    verb = STATE_SET_VERB[element.role]
    members = [
        e
        for e in observed.snapshot.elements
        if e.role == element.role and e.parent_id == element.parent_id
    ] or [element]
    tiers = [deny_tier_of(_deny_facts(action, m, observed, verb=verb)) for m in members]
    return _highest(*tiers)


def _deny_facts(action: ActionDoc, element: Element, observed: Observed, *, verb: str) -> DenyFacts:
    return DenyFacts(
        verb=verb,
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
        if held.now is None or held.typed is None:
            states.append((held, "unread"))
        elif held.now.filled:
            same = held.now.digest == held.typed.digest
            states.append((held, "held" if same else "changed"))
    return states


def _generated(action: ActionDoc, states: Sequence[tuple[PendingText, Held]]) -> bool:
    free_text = action.verb == "type" and action.param is None
    return free_text or any(not held.goal_span for held, _ in states)


def _text_checked(tally: _Tally, action: ActionDoc, state: Held) -> None:
    """A changed or unread text (the field no longer provably holds what Steer typed) is never a
    query, asks `text_changed` under a commit-none subgoal, and refuses a declared send or
    submit (principal re-review 2)."""
    if state in ("changed", "unread"):
        if action.commit in SENT_COMMITS:
            tally.refused = True
        elif action.commit == "none":
            tally.reasons.add("text_changed")


def _classified(  # noqa: PLR0913 - one submit: the tally, the action, its text and facts
    tally: _Tally,
    action: ActionDoc,
    observed: Observed,
    held: PendingText,
    state: Held,
    *,
    denied: bool,
) -> Risk:
    """One submit of `held`'s text, like its Enter. The query facts recorded at typing time count
    only while the current observation still reads them (#307 reviews): the field is a searchbox
    only while the AriaRole map was read and still names it `searchbox`, and in a Search landmark
    only while the landmark map still reads one. An unread map or another value fails closed. The
    form facts are computed again on this observation and combined with the type-time ones by OR
    ([V2.30] timing)."""
    _text_checked(tally, action, state)
    recorded = held.search
    roles = observed.aria_roles
    box = recorded.searchbox and roles is not None and roles.get(held.field) == "searchbox"
    still = recorded.in_search_landmark and search_landmark_of(observed, held.field) is not None
    goal_span = held.goal_span and state == "held"
    # #314, [V2.32]: the form is scanned only when every other condition still allows a query;
    # otherwise `classify` gives R2 whatever the form holds, and the unscanned facts read true
    # (#319 review S1-05: fail closed, as `search_facts` does)
    may_query = (box or still) and goal_span and action.commit == "none" and not denied
    secure, risky = form_facts(observed, held.field) if may_query else (True, True)
    now = dataclasses.replace(
        recorded,
        searchbox=box,
        in_search_landmark=still,
        secure_in_form=recorded.secure_in_form or secure,
        risky_in_form=recorded.risky_in_form or risky,
    )
    return classify(field=now, goal_span=goal_span, commit=action.commit, deny_word=denied)


def _enter(  # noqa: PLR0913 - the tally, the action, its target and the read facts
    tally: _Tally,
    action: ActionDoc,
    field: Element,
    observed: Observed,
    states: Sequence[tuple[PendingText, Held]],
    *,
    denied: bool,
) -> None:
    mine = next(((h, s) for h, s in states if h.field == field.runtime_id), None)
    if mine is None:
        tally.raise_to(
            classify(field=NO_SEARCH, goal_span=False, commit=action.commit, deny_word=denied)
        )
        return
    held, state = mine
    tally.raise_to(_classified(tally, action, observed, held, state, denied=denied))


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
        near = beside(observed, held.field, target)
        if near is False:
            continue
        tally.submit_parent = tally.submit_parent or _parent_kind(observed, held.field)
        if near is True:
            # [V2.28] item 5: decided by the field. A submit click within the [V2.22] scope of a
            # query-class field is classified as that field's Enter; the search landmark only
            # qualifies the field, never the button. A deny word on the target still commits.
            tier = _classified(tally, action, observed, held, state, denied=denied)
        else:
            # an unknown relation (a missing fact) is still a submit, and never a query: R2
            _text_checked(tally, action, state)
            tier = "R2"
        if _ORDER[tier] >= _ORDER["R2"]:
            tally.reasons.add("submit_pending_text")
        tally.raise_to(tier)


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
