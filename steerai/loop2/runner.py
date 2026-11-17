"""`run_task`: the step-3 v2 loop (V2 §5, [V2.11], [V2.12], [V2.18], [V2.24] item 3, [V2.28]
item 3, [V2.29] Q2, [V2.37], [V2.38]), behind `--loop v2`.

One controller call per turn; its actions run as a batch while the batch rule holds. Nothing at
R2 or above is dispatched in step 3: the task ends `blocked` / `commit_not_in_step` before any
write-ahead. Every outside effect goes through `Ports`; a port error ends the task `stopped` with
a taxonomy token and the evidence kept (RT-16).

The claim checks and the `navigated` auto proof are step 3c PR B (`verify.check_claim`,
`verify.judge`, whose verdict is `verify.auto_proof`'s); the auto proof is tried on every full
observation ([V2.67] A) when there is a change signal since a subgoal's baseline (the URL
changed, or the top Document was replaced), which §7.1 requires before anything else. The
Enter table on the watch's final report and the staleness rule are PR C (`enter_outcome`,
`stale_observation`).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, get_args

from steerai.action.dispositions import Delivery
from steerai.errors import ErrorCode, SteerError
from steerai.loop2.actuate import may_retry, press_allowed, type_focus_ok
from steerai.loop2.batch import batch_continues
from steerai.loop2.controller import (
    TOKEN,
    ControllerError,
    DecideRecord,
    decision_shape,
    focus_state,
)
from steerai.loop2.decision import (
    CLAIM_CLOSES,
    NOT_IN_STEP_VERBS,
    ActionDoc,
    Claim,
    Decision,
    Stop,
    SubgoalDoc,
    validate,
)
from steerai.loop2.form import ENTER_FIELDS, STATE_SET_VERB, scope_fields
from steerai.loop2.guards import (
    ActionKey,
    StrikeReason,
    Strikes,
    TurnFacts,
    Usage,
    budget_hit,
    made_progress,
    same_action,
    single_cause,
)
from steerai.loop2.history import (
    REFUSAL_TOKENS,
    ClaimResult,
    ClaimWord,
    Entered,
    FocusMovedAfterPress,
    HistoryRecord,
    LoadGuard,
    LogEntry,
    Proven,
    ProvenHow,
    RefusalWord,
    Refused,
    RunnerClaim,
    Vocab,
    WorldMoved,
    is_cause,
    log_role,
    record_ok,
    refused_shape,
    render,
)
from steerai.loop2.listing import ListingTurn, words_of
from steerai.loop2.loop import (
    AUDIT_COUNTS,
    AUDIT_FAILED,
    AUDIT_NO_CALL,
    COMMIT_RISK,
    INTAKE_COUNTS,
    PARENT_KINDS,
    READ_BACK_VERBS,
    URL_READS,
    ActResult,
    Audited,
    AuditView,
    CountFailed,
    Dispatch,
    IntakeLine,
    IntakeView,
    ParentKind,
    PendingText,
    PortInterrupted,
    Ports,
    ProofLine,
    Provenance,
    ReadKind,
    Reason,
    RiskWhy,
    RunnerAuditReason,
    Status,
    SufficiencyReason,
    TaskOutcome,
    TokenUsage,
    TurnView,
    UrlRead,
    WatchKind,
    WindowCovered,
    action_risk,
    calls_made,
    type_refusal,
    url_read,
    watch_kind,
)
from steerai.loop2.query import (
    EnterOutcome,
    FieldState,
    SearchFacts,
    field_state,
    query_outcome,
    search_facts,
    search_landmark_of,
    top_document_replaced,
)
from steerai.loop2.task import GoalSpan, Subgoal, Task, known_params
from steerai.loop2.turn_trace import (
    ActFact,
    CarrierFact,
    Gate,
    ParamProof,
    ProofAttempt,
    ReadFacts,
    RefusalFact,
    Skipped,
    TurnTrace,
    WatchFacts,
    closed,
    detail_token,
    positional,
    turn_record,
)
from steerai.loop2.turn_trace import ReadKind as ProofRead
from steerai.loop2.verify import (
    PAGE_CARRIERS,
    URL_CARRIERS,
    CheckToken,
    Judged,
    Readers,
    check_claim,
    field_held,
    holds_words,
    judge,
)
from steerai.orchestrator.contracts import RISKS, Risk
from steerai.orchestrator.loop import REUSE_FRESHNESS_S
from steerai.session.monitor import TAKEOVER_TOKENS
from steerai.verify.predicates import FIELD_ROLES
from steerai.world.effects import EffectReport, Observed, ValueFact, listing_delta, top_document
from steerai.world.elements import Element
from steerai.world.model import normalise_name
from steerai.world.observe2 import FIRST_READY_S, YOUNG_S, document_nodes
from steerai.world.reads import ReadCost
from steerai.world.watch import Watched

STRIKE_LIMIT: Final = 3
"""[V2.4]: the pooled set runs with a limit of 3, frozen before H7."""
RefusalKind = Literal["policy", "step", "model", "world"]
"""[V2.12], [V2.37] P5: every refusal replaces the latest one, whatever its kind. A policy
refusal ends `blocked` with its token; a step limit ends `blocked` with `not_in_step`; a model
fault and a `world` refusal (nothing delivered, the focus not where the press goes) keep the
stop's own value, recorded `after_refusal:<token>`."""
Moved = Literal["page", "target", "focus"]
"""[V2.43] (2): what moved after the observation the model saw."""
REFUSAL_KIND: Final[Mapping[str, RefusalKind]] = {
    "secure_field": "policy",
    "secure_unread": "policy",
    "text_changed": "policy",
    "not_in_step": "step",
    "not_delivered": "world",
    "press_not_at_focus": "world",
    "press_target_gone": "world",
    "press_target_changed": "world",
    "type_not_at_focus": "world",
    "field_not_empty": "world",
    "not_foreground": "world",
    "screen_changed_during_ask": "world",
    "composer_unreadable": "world",
    "composer_differs": "world",
    "ask_empty": "model",
    "ask_with_actions": "model",
    "ask_with_stop": "model",
    "commit_cannot_apply": "model",
    "commit_conflict": "model",
    "commit_undeclared": "model",
    "desc_bad": "model",
    "done_already": "model",
    "empty_decision": "model",
    "focus_needs_field": "model",
    "id_not_listed": "model",
    "intake_insufficient": "model",
    "key_missing": "model",
    "no_strict_shape": "model",
    "not_own_text": "model",
    "option_missing": "model",
    "page_without_param": "model",
    "param_missing": "model",
    "param_name_bad": "model",
    "param_not_span": "model",
    "param_unknown": "model",
    "repeat_send": "model",
    "state_without_param": "model",
    "stop_with_actions": "model",
    "subgoal_duplicate": "model",
    "subgoal_id_bad": "model",
    "subgoal_unknown": "model",
    "subgoals_after_first": "model",
    "subgoals_missing": "model",
    "target_missing": "model",
    "target_on_untargeted_verb": "model",
    "text_bad": "model",
    "text_missing": "model",
    "too_many_actions": "model",
    "too_many_subgoals": "model",
    "type_text_and_param": "model",
    "value_without_param": "model",
    "wanted_missing": "model",
    "wanted_not_state": "model",
    "field_missing": "model",
    "field_not_value": "model",
    "field_not_span": "model",
}
"""[V2.12], [V2.37] P5, [V2.74]: each refusal token's kind, declared for every token (no default;
a token without a kind fails a test). [V2.43]: a world fact the person-ask
step meets is `world`."""
KIND_ORDER: Final[tuple[RefusalKind, ...]] = ("policy", "step", "world", "model")
"""[V2.42] RT-29: several refusal reasons take the kind by this priority, never token order."""
CLOCK_DOMAIN_S: Final = 3600.0
"""RT-10: a snapshot stamped further than this from `Ports.clock` was stamped on another clock;
the loop refuses to compare them (the reuse age between batch actions would be meaningless)."""
LOAD_GUARD_S: Final = YOUNG_S
"""[V2.31], [V2.38] RT-24: one total per top Document, from when it was first seen: the waits
that are no strike on a growing or young page and the re-ask before giving up all end when the
Document is this old, or when the wall budget is spent. The flags are still reported after it.
The same number as a young page ([V2.35] (2)), so the two cannot drift; provisional."""
_R2: Final = RISKS.index("R2")
Identity = tuple[str, str, str]
"""[V2.11] decision 3, [V2.38] RT-03: the ledger identity (role, normalised name, automation id)."""


@dataclass(frozen=True)
class _Typed:
    """[V2.37] P2, [V2.38] RT-03: a field Steer typed into in this task, whatever subgoal typed
    it. Keyed by RuntimeId, found again by its ledger identity under the same top Document; the
    value it read right after a verified type (None: unread, or the type was not verified), the
    query facts at type time, and the text's provenance travel with the field."""

    field: tuple[int, ...]
    identity: Identity
    scope: tuple[int, ...] | None
    """The top Document's RuntimeId at type time: a replaced Document ends the pending text."""
    typed: ValueFact | None
    search: SearchFacts
    goal_span: bool


@dataclass(frozen=True)
class _Pending:
    """#335 review: a pending field as `_pending` found it: its `_Typed` record, the one element
    holding it (None: not found on a partial read, or several identity matches), and the value
    read then."""

    typed: _Typed
    element: Element | None
    value: ValueFact | None


@dataclass
class _ActCounts:
    """What the act rules saw, reported on the outcome (`TaskOutcome` has each field's rule):
    [V2.43] (1) presses after which focus left a state-set target, [V2.52] (b) observations
    with the focus unread or on nothing listed, [V2.51] (3) types not at
    the focus, [V2.52] (a) second Toggles fired, refused or settled on the re-read, [V2.51] (5)
    fields not empty and Backspaces on others' text."""

    focus_moved_after_press: int = 0
    observations_focus_unread: int = 0
    observations_focus_elsewhere: int = 0
    type_unfocused: int = 0
    second_toggles: int = 0
    second_toggles_refused: int = 0
    toggles_settled_on_reread: int = 0
    fields_not_empty: int = 0
    not_own_text: int = 0
    evidence_taken: int = 0
    field_mismatch: int = 0


@dataclass
class _ListingState:
    """[V2.54]: the handles the last Decision was shown (None: every bound handle) and each
    turn's listing counts."""

    shown: frozenset[int] | None = None
    turns: list[ListingTurn] = field(default_factory=list)
    seen: Observed | None = None
    """[V2.56]: the observation the model last saw: `new` and `changed` are against it."""


class _End(Exception):  # noqa: N818 - a control-flow signal, not an error
    """The task ends here with this status and cause."""

    def __init__(self, status: Status, cause: Reason | None) -> None:
        super().__init__(status)
        self.status = status
        self.cause = cause


@dataclass
class _Turn:
    """What one controller turn did, for the strike record."""

    action: ActionKey | None = None
    effect: EffectReport | None = None
    refused: bool = False
    ticked: bool = False
    claim_rejected: bool = False
    neutral: bool = False
    """Not a strike and not a reset ([V2.31], [V2.35] (2))."""
    sent: int = 0
    """The actions this turn dispatched."""
    stale_read: Observed | None = None
    """The world moved after the observation the model saw ([V2.42] RT-19, [V2.43] (2)): the
    staleness rule applies to this read."""
    moved: Moved = "page"
    """What moved: the page (top Document or URL), the named target, or the focus."""
    moved_after_send: bool = False
    """The world moved after a send in this turn: the turn is scored normally and the stale
    streak is left as it was, neither raised nor reset ([V2.43] confirmation)."""
    moved_handle: int | None = None
    """[V2.47]: the task handle of the element that moved (a target or the focus), if any."""
    focused: tuple[int, ...] | None = None
    """[V2.51] (3): the field a focus in this turn's batch put the focus on (read back True): a
    type after it is checked against it, not against the listing taken before it."""
    seen: Observed | None = None
    """[V2.67] D: the observation the model saw this turn."""
    shape: dict[str, Any] | None = None  # Any: JSON values (`decision_shape`)
    number: int = 0
    """[V2.67] D: the turn's number among the loop's turns, fixed when the turn is made."""
    listing: ListingTurn | None = None
    """This turn's own listing, as its decide reported it; None: unknown."""
    usage: TokenUsage | None = None
    refusals: list[RefusalFact] = field(default_factory=list)
    acts: list[ActFact] = field(default_factory=list)
    repair: bool = False
    repair_tokens: tuple[str, ...] = ()
    at: int = 0
    """[V2.67] D: the position in the batch of the action being handled."""
    ratings: dict[int, tuple[Risk, RiskWhy | None]] = field(default_factory=dict)
    """[V2.67] D: each rated action's tier and the rule that set it, by batch position."""
    traced: bool = False
    """[V2.67] D: this turn's record went to the trace port (or was dropped): once only."""
    watches: list[WatchFacts] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    proofs: list[ProofAttempt] = field(default_factory=list)
    """[V2.67] D: every proof attempt this turn, filled by the verifier's judgements."""


class _Handles:
    """[V2.38] RT-17: task-scoped listing handles. Each is bound to one RuntimeId for the whole
    task and never reassigned; a new RuntimeId (a re-rendered element) gets a new one. A handle
    takes the element's snapshot ordinal when that number was never used, else the next free
    number, so the numbers stay small. An element with no RuntimeId gets none."""

    def __init__(self) -> None:
        self.by_rid: dict[tuple[int, ...], int] = {}
        self.by_handle: dict[int, tuple[int, ...]] = {}

    def bind(self, observed: Observed) -> dict[int, int]:
        """Each element's handle, by its snapshot id, binding the RuntimeIds seen first now. A
        RuntimeId held by more than one element of the snapshot gets no handle there: the number
        could land on either (#330 review)."""
        out: dict[int, int] = {}
        shared = _shared_rids(observed)
        for element in observed.snapshot.elements:
            rid = element.runtime_id
            if not rid or rid in shared:
                continue
            if rid not in self.by_rid:
                handle = element.id
                if handle in self.by_handle:
                    handle = max(self.by_handle) + 1
                self.by_rid[rid] = handle
                self.by_handle[handle] = rid
            out[element.id] = self.by_rid[rid]
        return out

    def element(self, handle: int, observed: Observed) -> Element | None:
        """The one element the handle names in `observed`; None when it is gone or its RuntimeId
        is held by more than one element there."""
        rid = self.by_handle.get(handle)
        if rid is None:
            return None
        matches = [e for e in observed.snapshot.elements if e.runtime_id == rid]
        return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class _Ever:
    """[V2.38] RT-39: one type Steer sent in this task, never pruned (unlike pending text): the
    field and the words when typed by param (None: generated text, whose words are not kept).
    [V2.73]: the subgoal it was typed under is not kept; a label never decides evidence."""

    field: tuple[int, ...]
    words: str | None


@dataclass(frozen=True)
class _Proof:
    """[V2.39] (2): one param's accepted proof: the element by its task handle, read again when
    the subgoal closes."""

    handle: int
    kind: str


@dataclass(frozen=True)
class _Acted:
    """The last element acted on, for a press (§8.1: re-checked by role and name, P4)."""

    runtime_id: tuple[int, ...]
    role: str
    name: str


def run_task(task: Task, ports: Ports) -> TaskOutcome:
    """Run `task` to an outcome. Between batch actions the watch's after observation is reused
    only when it is a full-tier read, stable and within v1's R0/R1 freshness limit
    (`REUSE_FRESHNESS_S`, its age `clock() - snapshot.taken_at`); otherwise the loop re-observes
    ([V2.11] decision 6)."""
    return _Run(task, ports).go()


def stale_observation(decided_on: Observed, now: Observed) -> bool:
    """[V2.28] item 1: the top Document was replaced or the URL changed since the observation a
    Decision was made on. Only those two facts, never the whole signature: a ticker page would
    always look stale. A fact unread on both sides is no change; read on one side only, it is a
    change (#335 review): a top Document present on one side, or a URL read on one side where
    both observe the address bar (`url_observed`, as the observer's `url` fact)."""
    old, new = top_document(decided_on.snapshot), top_document(now.snapshot)
    if (old is None) != (new is None):
        return True
    if old is not None and new is not None and old.runtime_id != new.runtime_id:
        return True
    bar = decided_on.url_observed and now.url_observed
    if bar and (decided_on.url is None) != (now.url is None):
        return True
    return decided_on.url is not None and now.url is not None and decided_on.url != now.url


def enter_outcome(
    report: EffectReport, *, before: Observed, after: Observed, typed_field: FieldState
) -> EnterOutcome:
    """[V2.28] item 1, [V2.21]-[V2.24]: the Enter table, evaluated once on the watch's final
    report of a submit (the watch held it to a conclusive report). The text is carried by a URL,
    title or heading carrier, or by the results beside a URL carrier ([V2.29] Q1)."""
    carried = bool(report.carriers & (URL_CARRIERS | PAGE_CARRIERS))  # results alone: none
    return query_outcome(
        report,
        text_carried=carried,
        typed_field=typed_field,
        top_replaced=top_document_replaced(before, after),
    )


class _Run:
    def __init__(self, task: Task, ports: Ports) -> None:  # noqa: PLR0915 - the run's state, one field each
        self.task = task
        self.ports = ports
        self.subgoals: tuple[Subgoal, ...] = task.subgoals
        self.frozen = bool(task.subgoals)
        self.proven: set[str] = set()
        self.log: list[HistoryRecord] = []
        """[V2.46]: action entries and history events, in order (`TurnView.log`)."""
        self.history_dropped = 0
        """[V2.47]: lines that failed the history pattern (`_note`)."""
        self.counts = _ActCounts()
        self.listing = _ListingState()
        self.dispatched: list[Dispatch] = []
        self.model_calls = 0
        self.tokens = 0.0
        self.by_category = [0, 0, 0, 0, 0]
        """[V2.40] Q3, [V2.45]: input, output, 5-minute write, 1-hour write and read, over every
        reported call."""
        self.usage_incomplete = False
        self.watch_counts: dict[WatchKind, list[int]] = {k: [0, 0] for k in get_args(WatchKind)}
        """[V2.48]: per class, the watches and those that ended at their deadline."""
        self.exact_counts = 0
        self.estimates_low = 0
        """[V2.61]: turns with an exact count; turns whose bound was low."""
        self.rejections: tuple[str, ...] = ()
        """The shakedown's #3: the last rejected turn's closed tokens, for the final line."""
        self.strikes = Strikes(STRIKE_LIMIT)
        self.strike_log: list[tuple[StrikeReason, ...]] = []
        self.strike_words: list[frozenset[str]] = []
        """[V2.69] Two: each strike's refusal tokens, beside `strike_log`, for the single cause."""
        self.refusal: tuple[RefusalKind, str] | None = None
        self.typed: list[_Typed] = []
        self.pending: list[_Pending] = []
        """#335 review: the pending fields as the last `_pending` found them, for row 2."""
        self.baselines: dict[str, Observed] = {}
        self.first: Observed | None = None
        self.first_full: Observed | None = None
        """[V2.72]: the task's first full observation, every subgoal's baseline."""
        self.rebased: set[str] = set()
        """[V2.74] (#475): the non-commit subgoals another subgoal's close through a model claim
        rebased (`_rebase`); `rebased_open` counts those still open when the task ends. An auto
        proof's close rebases too, but is not counted: no claim order decided it."""
        self.rebased_commit: set[str] = set()
        """[V2.76]: as `rebased`, for commit subgoals."""
        self.last_acted: _Acted | None = None
        self.batch_last: tuple[ActionKey, EffectReport] | None = None
        self.fresh: dict[tuple[int, ...], ValueFact | None] = {}
        self.started = ports.clock()
        self.turn = _Turn()
        self.turns = 0
        """[V2.67] D: the turns the loop opened (its trace's turn numbers), never the calls."""
        self.turn_trace_dropped = 0
        """[V2.67] D: turn records the no-free-text check dropped."""
        self.turn_trace_errors = 0
        """#463 (1): turns whose record could not be built or written."""
        self.read_costs: list[ReadCost] = []
        self.reasked: set[tuple[int, ...]] = set()
        """[V2.38] RT-12: the top Documents already re-asked on (once per Document)."""
        self.handles = _Handles()
        self.ever: list[_Ever] = []
        self.proofs: dict[str, dict[str, _Proof]] = {}
        """[V2.39] (2): per value or state subgoal, the accepted proof of each param."""
        self.stale_streak = 0
        """[V2.28] item 1: stale observations in a row; the second is a strike."""
        self.stale_count = 0
        """Stale observations in the task, reported on the outcome."""
        self.standing: dict[str, list[tuple[RefusalKind, str, int]]] = {}
        """[V2.42] RT-30: per subgoal, the policy and step-limit refusals that stand until it is
        proven (kind, token, order); a proven subgoal's entries are ignored at the stop."""
        self.refused_seq = 0
        self.target_changes = 0
        """[V2.43] (2): targets re-rendered after the observation the model saw."""
        self.focus_losses = 0
        """[V2.43] (2): focus moved off a press's target after the listing showed it there."""
        self.unread_aids = 0
        self.proven_how: dict[str, ProvenHow] = {}
        """[V2.64] One: how each subgoal was proven (a claim, or the auto proof's token)."""
        self.audit: tuple[str, SufficiencyReason | RunnerAuditReason] | None = None
        """[V2.64] One: the final audit's outcome and reason, when it ran."""
        self.audit_counts: dict[str, int] = dict.fromkeys(AUDIT_COUNTS, 0)
        """[V2.69] One, packet rev 4 item 4: the audit's counts (`_audit`)."""
        self.intake_counts: dict[str, int] = dict.fromkeys(INTAKE_COUNTS, 0)
        """[V2.68] One, packet rev 4 item 4: the intake check's counts (`_intake_passes`)."""
        self.intake_memo: dict[str, bool] = {}
        """Packet rev 5 item 1 (P4): each declared set's verdict by its digest, passed or
        rejected; a set already judged makes no new call."""
        self.free_texts: list[str] = []
        """Packet rev 5 item 5: the free text of each `type` Steer sent with `text` (not a
        param), kept in memory only, masked out of the audit's view."""
        self.query_combobox_listed = False
        self.typed_into_combobox = False
        """[V2.64] Two: the two ComboBox facts of the task, no judgement."""
        """#301: unread automation ids over every observation taken in (`_in_domain`)."""
        self.partial_page_calls = 0
        self.judged: Observed | None = None
        """§7.1: the last read the auto proof judged (`_auto_proofs`)."""
        self.saved_calls = 0
        """[V2.67] A: turns the turn-start judgement closed the task, so no model call was made."""
        self.next_read: ProofRead = "turn_start"
        """[V2.67] D: what the next turn-start judgement runs on (`_stale` marks a re-read)."""
        self.pre_task_draft_clicks = 0
        """[V2.64] Two: counted at each non-field click (`_pre_task_draft`)."""
        """[V2.35]: model calls made on a growing or young observation (`_decide`)."""
        self.refusal_counts: dict[RefusalWord, int] = {}
        """[V2.46], [V2.63] (e): every refusal by its token (`_refuse`)."""
        self.submit_parents: dict[ParentKind, int] = dict.fromkeys(PARENT_KINDS, 0)
        """[V2.25]: the submit rule's firings by the field's parent kind (`_risk`)."""
        self.url_reads: dict[UrlRead, int] = dict.fromkeys(URL_READS, 0)
        """Stage 2 plan §5: every observation taken in, by why its URL is what it is."""

    # ---- the outcome ----------------------------------------------------------------------

    def go(self) -> TaskOutcome:
        try:
            self._loop()
        except _End as end:
            return self._outcome(end.status, end.cause)
        except PortInterrupted:
            return self._after_port_interrupt()
        except WindowCovered:  # [V2.62]: covered, read before any wait for a page
            return self._outcome("stopped", Reason("window_covered"))
        except NotImplementedError:  # packet §7: a later step's stub never falls through
            return self._outcome("stopped", Reason("internal_stub_reached"))
        except (SteerError, ControllerError) as exc:  # RT-16: a port failed; keep the evidence
            code = exc.code.value if isinstance(exc.code, ErrorCode) else exc.code
            return self._outcome("stopped", Reason(f"port_{code}"[:64]))
        raise AssertionError("the loop ends only through _End")  # pragma: no cover

    def _after_port_interrupt(self) -> TaskOutcome:
        """[V2.40] Q4: `PortInterrupted` has no authority. The switch is read again: latched, the
        task stops with its token; not latched, the port failed (`interrupted_unlatched`)."""
        try:
            self._check_interrupt()
        except _End as end:
            return self._outcome(end.status, end.cause)
        return self._outcome("stopped", Reason("port_interrupted_unlatched"))

    def _outcome(self, status: Status, cause: Reason | None) -> TaskOutcome:
        self._trace_turn()  # [V2.67] D: the turn the task ended in, before its counts
        return TaskOutcome(
            status=status,
            cause=cause,
            wall_s=self.ports.clock() - self.started,
            dispatched=tuple(d.redacted() for d in self.dispatched),
            model_calls=self.model_calls,
            strikes=tuple(self.strike_log),
            tokens=self.tokens,
            cost_usd=self._cost(),
            usage_incomplete=self.usage_incomplete,
            watch_deadlines=tuple((k, w, d) for k, (w, d) in self.watch_counts.items()),
            rejections=self.rejections,
            usage=TokenUsage(*self.by_category),
            exact_counts=self.exact_counts,
            estimates_low=self.estimates_low,
            read_costs=tuple(self.read_costs),
            stale_observations=self.stale_count,
            target_changes=self.target_changes,
            focus_losses=self.focus_losses,
            unread_automation_ids=self.unread_aids,
            url_reads=tuple(self.url_reads.items()),
            audit=self.audit,
            **self.audit_counts,
            **self.intake_counts,
            query_combobox_listed=self.query_combobox_listed,
            typed_into_combobox=self.typed_into_combobox,
            partial_page_calls=self.partial_page_calls,
            pre_task_draft_clicks=self.pre_task_draft_clicks,
            saved_calls=self.saved_calls,
            refusals=tuple(self.refusal_counts.items()),
            submit_parents=tuple(self.submit_parents.items()),
            history_dropped=self.history_dropped,
            turn_trace_dropped=self.turn_trace_dropped,
            turn_trace_errors=self.turn_trace_errors,
            listings=tuple(self.listing.turns),
            focus_moved_after_press=self.counts.focus_moved_after_press,
            observations_focus_unread=self.counts.observations_focus_unread,
            observations_focus_elsewhere=self.counts.observations_focus_elsewhere,
            type_unfocused=self.counts.type_unfocused,
            second_toggles=self.counts.second_toggles,
            second_toggles_refused=self.counts.second_toggles_refused,
            toggles_settled_on_reread=self.counts.toggles_settled_on_reread,
            fields_not_empty=self.counts.fields_not_empty,
            not_own_text=self.counts.not_own_text,
            evidence_taken=self.counts.evidence_taken,
            field_mismatch=self.counts.field_mismatch,
            rebased_open=len(self.rebased - self.proven),
            rebased_open_commit=len(self.rebased_commit - self.proven),
        )

    def _cost(self) -> float | None:
        """[V2.37] P6, [V2.40] Q3: each category times its own rate; None when no price is
        configured or a used category has no rate ("not configured", never 0.0). With some
        usage missing it is NaN: the cost budget trips, as the token budget does."""
        price = self.task.budgets.price
        if price is None:
            return None
        total = 0.0
        for used, rate in zip(self.by_category, price.rates(), strict=True):
            if used and rate is None:
                return None
            total += used * (rate or 0.0)
        return math.nan if self.usage_incomplete else total

    def _stopped(self, stop: Stop) -> _End:
        """[V2.12], [V2.37] P5: the status after a stop, from the latest refusal since the last
        progress."""
        standing = [
            entry
            for sid, entries in self.standing.items()
            if sid not in self.proven
            for entry in entries
        ]
        if standing:  # [V2.42] RT-30: by the RT-29 order, then the latest
            kind, token, _seq = min(standing, key=lambda e: (KIND_ORDER.index(e[0]), -e[2]))
            return _End("blocked", Reason(token if kind == "policy" else "not_in_step"))
        if self.refusal is None:
            return _End(stop, Reason("controller_stop"))
        kind, token = self.refusal
        if kind == "policy":
            return _End("blocked", Reason(token))
        if kind == "step":
            return _End("blocked", Reason("not_in_step"))
        return _End(stop, Reason(token, after_refusal=True))

    # ---- the loop -------------------------------------------------------------------------

    def _loop(self) -> None:
        observed = self._observe()
        self.first = observed
        while True:
            self._check_interrupt()
            self._check_budgets()
            if _no_page(observed):  # first: a frame-only page is never `empty_listing`
                observed = self._page_not_ready(observed)
                continue
            if not self.handles.bind(observed):
                observed = self._nothing_listed(observed)
                continue
            self.turns += 1  # [V2.67] D: turns, not calls (a repaired turn is two calls)
            # [V2.67] A: a close at turn start is this turn's progress; made once, never
            # replaced ([V2.67] D: the trace's facts are set on it as they arrive)
            self.turn = _Turn(seen=observed, number=self.turns)
            self._judge_turn_start(observed)
            try:
                decision = self._decide(observed)
            except ControllerError as rejected:
                observed = self._rejected_turn(rejected, observed)
                self._trace_turn()  # [V2.67] D: one record per rejected turn
                continue
            if self.ports.turn_trace is not None:  # #444: no trace work without a sink
                # #457 review: shaped before the intake check, so a refused intake turn's record
                # carries the Decision it judged (None means no call)
                self.turn.shape = decision_shape(
                    decision,
                    known_subgoals=known_params(self.subgoals),
                    tokens=(),
                    turn=self.turn.number,
                    attempt=0,
                )
            if not self._intake_passes(decision):  # [V2.68] One: refused before anything freezes
                observed = self._record_strike(observed)
                self._trace_turn()  # [V2.67] D: one record per refused intake turn (#457 P5)
                continue
            self._check_interrupt()  # #326, [V2.40] Q4: a Decision after the kill is discarded
            fresh = self._observe()
            if fresh.read_cost is not None:
                self.read_costs.append(fresh.read_cost)  # [V2.34]: this re-read is paid for too
            if stale_observation(observed, fresh):
                self._trace_turn()
                observed = self._stale(fresh, moved="page")
                continue
            loading = (observed.growing or observed.young) and self._guard_left(observed) > 0
            observed = self._turn(decision, observed, fresh)
            if self.turn.stale_read is not None:
                self._trace_turn()
                observed = self._stale(self.turn.stale_read, moved=self.turn.moved)
                continue
            if not self.turn.moved_after_send:
                self.stale_streak = 0  # a turn that ran with nothing moved under it ends it
            waits = bool(decision.actions) and all(a.verb == "wait" for a in decision.actions)
            if loading and waits and self.turn.sent == len(decision.actions):
                # [V2.31], [V2.35] (2), P3: a wait chosen on a growing or young page, dispatched
                # in full with nothing refused, is no strike (more may arrive)
                self.turn.neutral = self.turn.neutral or not self.turn.refused
            observed = self._record_strike(observed)
            self._trace_turn()

    def _rejected_turn(self, rejected: ControllerError, observed: Observed) -> Observed:
        """[V2.68]: a Decision the controller still rejected after its repair is a refused turn,
        never the end of the task. Its tokens are refusals (history lines and counts), the turn
        takes one strike, and nothing is frozen, so an intake turn stays one; the task ends
        through the strike limit or a budget. An error with no call made, or with no token the
        history can show, keeps the port-failure path."""
        self._check_interrupt()
        tokens = [t for t in rejected.tokens if t in REFUSAL_TOKENS]
        if rejected.code != "rejected" or rejected.record is None or not tokens:
            raise rejected
        # on the turn the loop opened ([V2.67] A): a close at its start still counts
        self._refuse(*tokens)  # type: ignore[arg-type]  # each one checked against the set above
        if not self.frozen:  # [V2.69] Three: the next intake turn sees what it declared
            self._note(refused_shape(rejected.shape))
        return self._record_strike(observed)

    def _observe(self) -> Observed:
        return self._in_domain(self.ports.observe())

    def _page_not_ready(self, observed: Observed) -> Observed:
        """[V2.44] (2): a browser frame with no Document, or an empty one, is never handed to the
        model in steps 3 to 6 (step 7 changes this: once `open` exists, a blank page is
        actionable through the address bar). The task's first observation already waited out
        the first-ready deadline ([V2.31], through `first_observation`); later in the task the
        page is held (the settle port) until it has content, ending on the `FIRST_READY_S`
        deadline after it went blank (or the wall budget), never on a count of holds; a hold
        the clock did not advance across ends it at once, so a stopped clock never spins.
        Still blank: `page_not_ready` (a Steer stop, not done); a gone window is infrastructure."""
        if observed is not self.first:
            deadline = self.ports.clock() + FIRST_READY_S
            while (left := min(deadline - self.ports.clock(), self._wall_left())) > 0:
                started = self.ports.clock()
                observed = self._in_domain(self.ports.settle(observed, left))
                self._check_interrupt()
                if not _no_page(observed):
                    return observed
                if self.ports.clock() <= started:
                    break  # the clock did not move across the hold
        self._check_budgets()  # a budget that ended the wait is the end, not `page_not_ready`
        if _window_gone(observed):
            raise _End("stopped", Reason("window_gone"))
        raise _End("cannot", Reason("page_not_ready"))

    def _wall_left(self) -> float:
        return self.task.budgets.max_wall_s - (self.ports.clock() - self.started)

    def _nothing_listed(self, observed: Observed) -> Observed:
        """[V2.42] RT-40: the model is never called on an empty listing. The load guard first,
        once per top Document; after it, a live window is `empty_listing` (a perception limit,
        not done) and a gone window is `window_gone` (infrastructure, under the voiding rule)."""
        held = self._hold(observed)
        if held is not None:
            return held
        if _window_gone(observed):
            raise _End("stopped", Reason("window_gone"))
        raise _End("cannot", Reason("empty_listing"))

    def _in_domain(self, observed: Observed) -> Observed:
        """RT-10, RT-11: every observation is stamped by the one `stamp` port, on `Ports.clock`.
        Its `Snapshot.taken_at` must be in that clock's domain, or the reuse age means nothing.
        A `document_since` far in the past is legitimate (a long-lived stamp); one in the future
        beyond `CLOCK_DOMAIN_S`, or not finite, is another clock (small skew is clamped by the
        load guard)."""
        observed = self.ports.stamp(observed)
        if self.first_full is None and observed.tier == "full":
            self.first_full = observed  # [V2.72]: every subgoal's baseline
        self.unread_aids += observed.snapshot.unread_automation_id_count  # #301: counted
        self.url_reads[url_read(observed)] += 1
        focus = focus_state(observed.snapshot, observed.focus)  # [V2.52] (b): counted
        self.counts.observations_focus_unread += focus == "unread"
        self.counts.observations_focus_elsewhere += focus == "elsewhere"
        now = self.ports.clock()
        if not abs(now - observed.snapshot.taken_at) <= CLOCK_DOMAIN_S:
            raise _End("stopped", Reason("clock_domain"))
        since = observed.document_since
        if since is not None and not since - now <= CLOCK_DOMAIN_S:  # NaN and inf fail it too
            raise _End("stopped", Reason("clock_domain"))
        return observed

    def _decide(self, observed: Observed) -> Decision:
        view = TurnView(
            observed=observed,
            log=tuple(self.log),
            first_turn=not self.frozen,
            handles=self.handles.bind(observed),
            subgoals=self.subgoals,
            proven=frozenset(self.proven),
            issued=frozenset(self.handles.by_handle),
            keep=self._always_keep(),
            calls_left=self.task.budgets.max_model_calls - self.model_calls,
            **self._delta(observed),
        )
        self.listing.seen = observed
        try:
            decided = self.ports.decide(view)
        except (PortInterrupted, SteerError, ControllerError) as exc:
            if isinstance(exc, ControllerError):
                self.rejections = tuple(t for t in exc.tokens if TOKEN.fullmatch(t))
                self.exact_counts += exc.exact_counted
                self.estimates_low += exc.estimate_low
            record = exc.record if isinstance(exc, ControllerError) else None
            if isinstance(exc, ControllerError) and record is None:
                raise  # no call was made: nothing billed
            made = (
                (record.calls if record else 0)
                if isinstance(exc, ControllerError)
                else calls_made(exc)
            )
            if made == 0:
                raise  # #468: a count or a wait given up before any send: no call, nothing billed
            # invariant 9, [V2.68], #489: every request the turn sent, a repair included
            self.model_calls += made
            if isinstance(exc, ControllerError):  # [V2.67] D: the failed turn's facts
                self.turn.usage = exc.usage
                if isinstance(record, DecideRecord):
                    self.turn.repair = record.repaired
                    self.turn.repair_tokens = record.rejections
            self.partial_page_calls += observed.growing or observed.young
            counts = _counts(exc.usage) if isinstance(exc, ControllerError) else None
            if counts is None:
                # [V2.41] (1): an abandoned or failed call is billed but never reported: it is
                # counted, and the usage is a lower bound, never zero
                self.tokens = math.nan
                self.usage_incomplete = True
            else:  # a rejected turn's calls reported their usage: counted, never unknown
                self.tokens += sum(counts)
                self.by_category = [a + b for a, b in zip(self.by_category, counts, strict=True)]
            raise
        self.model_calls += decided.calls  # invariant 9, [V2.68]: a repaired turn is two calls
        made = decided.calls > 0  # [V2.71] (a): a Decision no model made spends nothing
        if not made and self.turns != 1:  # #454 round 2 (2): only the task's first turn
            raise ValueError("no_call_after_the_first_turn")
        self.partial_page_calls += made and (observed.growing or observed.young)  # [V2.35]
        self.turn.usage = decided.usage  # [V2.67] D: this turn's own facts
        self.turn.listing = decided.listing
        self.turn.repair = decided.calls > 1
        self.turn.repair_tokens = decided.rejections
        self.exact_counts += decided.exact_counted
        self.estimates_low += decided.estimate_low
        if made:
            self._spend(decided.usage)
        self.listing.shown = None if decided.listing is None else decided.listing.listed
        if not self.query_combobox_listed:
            self.query_combobox_listed = _query_combobox_listed(
                observed, view.handles, self.listing.shown
            )
        if decided.listing is not None:
            self.listing.turns.append(decided.listing)
        return decided.decision

    def _spend(self, usage: TokenUsage | None) -> None:
        """A reported call's usage into the task's totals; unknown makes them a lower bound."""
        counts = _counts(usage)
        if counts is None:
            self.tokens = math.nan  # never zero: the token budget trips ([V2.37] P6)
            self.usage_incomplete = True
        else:
            self.tokens += sum(counts)
            self.by_category = [a + b for a, b in zip(self.by_category, counts, strict=True)]

    def _delta(self, observed: Observed) -> dict[str, Any]:  # Any: TurnView's own field types
        """[V2.56]: `new` and `changed` against the observation the model last saw (never the
        listing), by the observer's own comparison; none on the first turn or after a replaced
        Document."""
        seen = self.listing.seen
        delta = None if seen is None else listing_delta(seen, observed)
        if delta is None:
            return {"changes": "no_comparison"}
        return {
            "new": delta.new,
            "changed": delta.changed,
            "changes": "compared" if delta.states_read else "states_unread",
        }

    def _always_keep(self) -> frozenset[tuple[int, ...]]:
        """[V2.54] (1): every element acted on in this task and every field holding pending text,
        by RuntimeId (the focus is the observation's own)."""
        acted = {d.runtime_id for d in self.dispatched if d.runtime_id}
        pending = {p.typed.field for p in self.pending}
        return frozenset(acted | pending)

    def _turn(self, decision: Decision, observed: Observed, fresh: Observed) -> Observed:
        listed = frozenset(self.handles.bind(observed).values())
        if self.listing.shown is not None:  # [V2.54]: a cut element keeps its handle, unlisted
            listed &= self.listing.shown
        first = not self.frozen
        goal = self.task.goal
        tokens = validate(
            decision,
            listed_ids=listed,
            goal=goal,
            first_turn=first,
            known_subgoals=known_params(self.subgoals) if self.frozen else None,
        )
        if tokens:
            self._refuse(*tokens, subgoal=_step_subgoal(decision))
            return observed
        if first and decision.subgoals:
            # packet rev 5 §4: subgoals freeze only on a turn that is not stale, after validate
            # and the intake check (`_intake_passes`) both passed
            self._intake(decision)
        self._claims(decision.claims, observed)
        self._check_done(observed)
        if decision.ask is not None:
            raise _End("asked", Reason("controller_ask"))
        if decision.stop is not None:
            held = self._hold(observed) if decision.stop == "cannot" else None
            if held is None:
                raise self._stopped(decision.stop)
            self.turn.neutral = True
            return held
        return self._batch(decision.actions, observed, fresh)

    def _cited(self, claim: Claim, observed: Observed) -> Claim:
        """The claim with its handles read as this snapshot's elements; a handle with no element
        here reads as an id no snapshot holds (`id_not_listed`)."""
        ids = []
        for handle in claim.evidence:
            element = self.handles.element(handle, observed)
            ids.append(-1 if element is None else element.id)
        return claim.model_copy(update={"evidence": ids})

    def _intake(self, decision: Decision) -> None:
        """[V2.11] decision 1: the first valid Decision fills the subgoals once; code freezes
        them."""
        docs = decision.subgoals or []
        goal = self.task.goal
        self.subgoals = tuple(
            Subgoal(
                id=doc.id,
                desc=doc.desc,
                params={name: _span(goal, text) for name, text in doc.params.items()},
                closes=doc.closes,
                commit=doc.commit,
                wanted=doc.wanted,
                field=doc.field,
            )
            for doc in docs
        )
        self.frozen = True

    def _subgoal(self, sid: str) -> Subgoal:
        return next(s for s in self.subgoals if s.id == sid)

    def _baseline(self, sid: str) -> Observed:
        """[V2.38] RT-39 as [V2.72] tightens it: the task's first full observation, for every
        subgoal; it never moves later, except where [V2.72] Two moves it (`_rebase`). Which
        subgoal an action was dispatched under plays no part: the model assigns those labels."""
        if sid in self.baselines:
            return self.baselines[sid]
        if self.first_full is None:  # `observe` is a full read, taken before the first decide
            raise AssertionError("no full observation")  # #473 review: never a poll that moves
        return self.first_full

    def _claims(self, claims: Sequence[Claim], observed: Observed) -> None:
        """[V2.74] (#475): the claims of one Decision, judged in the subgoals' declaration order
        (stable for one subgoal's own), so the model's listing never decides which close
        rebases which. An unknown subgoal sorts last; `validate` has refused it before."""
        order = {s.id: i for i, s in enumerate(self.subgoals)}
        for claim in sorted(claims, key=lambda c: order.get(c.subgoal, len(order))):
            self._claim(claim, observed)

    def _claim(self, claim: Claim, observed: Observed) -> None:
        """One claim: code's refusals first (`_refused`), then the claim check. A page claim
        closes when it proves every param; a value or state claim records the proof of one param,
        and the subgoal closes when every param's proof reads true again now."""
        subgoal = self._subgoal(claim.subgoal)
        param = _proved_param(claim, subgoal)
        refused = self._refused(claim, subgoal, param, observed)
        if refused is not None:
            self._reject(subgoal, (refused,))
            return
        rejected = check_claim(
            self._cited(claim, observed),
            subgoal,
            self._baseline(subgoal.id),
            observed,
            readers=self._readers(observed),
            typed=self._typed_fields(),
            acted=self._acted(),
            echo=self._echo(subgoal),
            allow_file=self._allow_file(),
        )
        if rejected:
            if "field_mismatch" in rejected:
                self.counts.field_mismatch += 1
            self._reject(subgoal, rejected)
            return
        if subgoal.closes == "page":
            self._tick(subgoal, how="claim", at=observed)
            return
        proofs = self.proofs.setdefault(subgoal.id, {})
        proofs[param or ""] = _Proof(claim.evidence[0], claim.kind)
        if set(proofs) >= set(subgoal.params) and self._still(subgoal, observed):
            self._tick(subgoal, how="claim", at=observed)
            return
        self._note(ClaimResult(subgoal.id, "accepted"))  # [V2.47]: one param held, not closed

    def _refused(  # noqa: PLR0911 - one return per refusal, in the rules' order
        self, claim: Claim, subgoal: Subgoal, param: str | None, observed: Observed
    ) -> RunnerClaim | CheckToken | None:
        """Code's refusals before the claim check, first match wins: a commit subgoal is never
        closed in step 3 (RT-06); the claim's kind must prove the subgoal's closing kind
        ([V2.39]); a value or state claim must name one param and cite exactly one element (#422:
        the claim check's own count tokens, before anything reads it), an element that proves no
        other param (#334 review), and a state claim's element must hold the param's words
        ([V2.40] Q7). [V2.73]: nor one that is already the evidence of a closed subgoal of the
        same kind sharing a span (`evidence_taken`, counted)."""
        if subgoal.commit != "none":
            return "standard_only"
        if CLAIM_CLOSES.get(claim.kind) != subgoal.closes:
            return "kind_mismatch"
        if subgoal.closes == "page":
            return None
        if param is None:
            return "param_missing"
        if len(claim.evidence) != 1:
            return "too_much_evidence" if claim.evidence else "evidence_missing"
        if self._reused(subgoal, param, claim.evidence[0]):
            return "evidence_reused"
        if self._taken(subgoal, param, claim.evidence[0]):
            self.counts.evidence_taken += 1
            return "evidence_taken"
        named = claim.kind != "state" or self._named(subgoal, param, claim.evidence[0], observed)
        return None if named else "words_missing"

    def _taken(self, subgoal: Subgoal, param: str, handle: int) -> bool:
        """[V2.73], extending [V2.40]'s one-handle rule across subgoals: an element that is the
        evidence of a closed subgoal cannot be the evidence of another subgoal of the same kind
        whose param shares a span with that closed subgoal's (`spans_shared`)."""
        span = subgoal.params[param]
        for other in self.subgoals:
            if other.id == subgoal.id or other.id not in self.proven:
                continue
            if other.closes != subgoal.closes:
                continue
            for name, proof in self.proofs.get(other.id, {}).items():
                shared = name in other.params and spans_shared(other.params[name], span)
                if proof.handle == handle and shared:
                    return True
        return False

    def _named(self, subgoal: Subgoal, param: str, handle: int, observed: Observed) -> bool:
        """[V2.40] Q7: a state claim proves a param only when the cited element's name, or its
        group name as v1 computes it, holds every word of the param. An unlisted handle is left
        to the claim check (`id_not_listed`)."""
        element = self.handles.element(handle, observed)
        return element is None or holds_words(
            observed.snapshot, element, subgoal.params[param].text
        )

    def _reused(self, subgoal: Subgoal, param: str, handle: int) -> bool:
        """#334 review: one element proves at most one param of a subgoal; the model's param is
        only a hint, so the same handle cited for a second param proves nothing more."""
        proofs = self.proofs.get(subgoal.id, {})
        return any(p.handle == handle for name, p in proofs.items() if name != param)

    def _still(self, subgoal: Subgoal, observed: Observed) -> bool:
        """[V2.39] (2): every param's proof read again now, on this observation: a subgoal
        never closes on a subset, nor on a read from an earlier turn."""
        readers = self._readers(observed)
        for param, proof in self.proofs.get(subgoal.id, {}).items():
            element = self.handles.element(proof.handle, observed)
            if element is None:
                return False
            if proof.kind == "state":
                words = holds_words(observed.snapshot, element, subgoal.params[param].text)
                if not words or subgoal.wanted is None:
                    return False
                if readers.read_state(element, subgoal.wanted) is not True:
                    return False
                continue
            closed = readers.read_state(element, "expanded") is not True  # [V2.85]
            want = readers.fact_of(subgoal.params[param].text)
            if not (
                field_held(observed.snapshot, element, subgoal)
                and closed
                and readers.read_value(element) == want
            ):
                return False
        return True

    def _reject(self, subgoal: Subgoal, tokens: Sequence[ClaimWord]) -> None:
        self._notes(ClaimResult(subgoal.id, t) for t in tokens)
        self.turn.claim_rejected = True

    def _note(self, record: HistoryRecord) -> None:
        """[V2.47], [V2.49]: every history record is checked against the task's vocabulary (its
        subgoals and the handles it issued) before the model can see it; one that fails is
        dropped and counted, never shown."""
        vocab = Vocab(known_params(self.subgoals), frozenset(self.handles.by_handle))
        if record_ok(record, vocab):
            self.log.append(record)
            if self.ports.turn_trace is not None:  # #444: no trace work without a sink
                ids = [s.id for s in self.subgoals]  # [V2.67] D (a): positions, never names
                self.turn.history.append(render(positional(record, ids, vocab.known)))
        else:
            self.history_dropped += 1

    def _notes(self, records: Iterable[HistoryRecord]) -> None:
        for record in records:
            self._note(record)

    def _handle_of(self, element: Element | None) -> int | None:
        """[V2.47]: an element's task handle, for a line about that element; None without one."""
        if element is None or not element.runtime_id:
            return None
        return self.handles.by_rid.get(element.runtime_id)

    def _allow_file(self) -> bool:
        """[V2.39]: a `file:` page counts only when the task started on one."""
        first = self.first
        address = None if first is None else (first.document_url or first.url)
        return address is not None and address.lower().startswith("file:")

    def _tick(self, subgoal: Subgoal, *, how: ProvenHow, at: Observed) -> None:
        """A proven subgoal, closed on the observation `at`. RT-06, §8.8: never a commit subgoal
        (`_refused` stops a claim on one, `_auto_proofs` skips it; the strict tier is step 4).
        [V2.74]: code's own proof is never a refusal, so nothing here can refuse."""
        if subgoal.commit != "none":
            raise AssertionError("auto proof reached a commit subgoal")
        self.proven.add(subgoal.id)
        self.proven_how[subgoal.id] = how
        self.turn.ticked = True
        self.refusal = None
        self._note(Proven(subgoal.id, how))
        self._rebase(subgoal, at, by_claim=how == "claim")

    def _rebase(self, closed: Subgoal, at: Observed, *, by_claim: bool = False) -> None:
        """[V2.72] Two: one event cannot close two subgoals of the same kind. Every other open
        subgoal with the same closing kind and a shared param span takes `at` as its baseline,
        so its evidence must be newer. Same kind only: a value and a page subgoal sharing a span
        may both close on one type (a field's digest and a page's carriers differ)."""
        for other in self.subgoals:
            if other.id in self.proven or other.closes != closed.closes:
                continue
            if _shares_span(closed, other):
                self.baselines[other.id] = at
                if by_claim:
                    (self.rebased if other.commit == "none" else self.rebased_commit).add(other.id)

    def _check_done(self, observed: Observed) -> None:
        """A task with zero subgoals never ends `done` ([V2.11] decision 1). [V2.69] One: every
        task about to end done is audited first, whatever closed its subgoals, and only the
        audit's allow ends done."""
        if self.subgoals and all(s.id in self.proven for s in self.subgoals):
            self._audit(observed, self._proof_lines())
            raise _End("done", None)

    def _proof_lines(self) -> tuple[ProofLine, ...]:
        """[V2.68], packet rev 5 §5: each proven subgoal by position (never its id): closes, how,
        kind, the closed token of what code read, and its params by sorted position with their
        spans of the request."""
        lines: list[ProofLine] = []
        for position, subgoal in enumerate(self.subgoals, 1):
            how = self.proven_how.get(subgoal.id, "claim")
            proofs = self.proofs.get(subgoal.id, {})
            kinds = {p.kind for p in proofs.values()}
            kind = (
                (next(iter(kinds)) if len(kinds) == 1 else "mixed")
                if proofs
                else ("navigated" if how == "claim" else how)
            )
            params = tuple((n, param.text) for n, param in numbered_params(subgoal.params))
            lines.append((position, subgoal.closes, how, kind, _read_token(subgoal, how), params))
        return tuple(lines)

    def _intake_passes(self, decision: Decision) -> bool:
        """[V2.68] One, [V2.69] Two and Four, packet rev 5 §4: a first Decision whose declared
        subgoals are all value or state (no ask, no stop) is checked once for sufficiency on the
        request and the subgoals alone, before anything freezes. A set already judged takes its
        verdict from the memo with no call. A spent budget pauses with its token, no call. A
        failed call made ends `blocked/intake_failed`; a port error takes decide's path. An
        insufficient set is refused (`intake_insufficient`): one strike, nothing frozen,
        nothing dispatched. True: the Decision goes on."""
        subs = decision.subgoals or []
        due = not self.frozen and bool(subs) and decision.ask is None and decision.stop is None
        intake = self.ports.intake
        if not due or intake is None or any(s.closes not in {"value", "state"} for s in subs):
            return True
        lines = _intake_lines(subs)
        key = hashlib.sha256(
            repr([(c, p, w, f) for _pos, c, p, w, f in lines]).encode()
        ).hexdigest()
        if key in self.intake_memo:
            if self.intake_memo[key]:
                return True
            self.intake_counts["intake_memo_refused"] += 1
            self._refuse("intake_insufficient")
            self._note(refused_shape([s.closes for s in subs]))  # [V2.69] Three
            return False
        self._intake_budget()
        self._check_interrupt()  # #457 P2: a latched switch sends nothing and counts nothing
        judged = self._intake_call(intake, IntakeView(subgoals=lines))
        passed = judged.sufficient and judged.reason == "facts_finish_request"
        self.intake_memo[key] = passed
        self.intake_counts["intake_passed" if passed else "intake_rejected"] += 1
        self._check_interrupt()  # #326: a verdict after the kill is discarded, its counts kept
        if not passed:
            self._refuse("intake_insufficient")
            self._note(refused_shape([s.closes for s in subs]))  # [V2.69] Three
        return passed

    def _intake_budget(self) -> None:
        """[V2.69] Four: a budget spent before the intake call: no call, `paused`, counted."""
        used = Usage(
            actions=len(self.dispatched),
            model_calls=self.model_calls,
            cost_usd=self._cost(),
            wall_s=self.ports.clock() - self.started,
            tokens=self.tokens,
        )
        hit = budget_hit(self.task.budgets, used)
        if hit is not None:
            self.intake_counts["intake_budget_stops"] += 1
            raise _End("paused", Reason(hit))

    def _intake_call(self, intake: Callable[[IntakeView], Audited], view: IntakeView) -> Audited:
        """Packet rev 5 item 1 (failures): no call (over the bound) keeps the no-call path; a port
        error or an interrupt takes decide's path, counted as a failed call; an unusable reply
        (unparsed, schema-invalid, a provider refusal) ends `blocked/intake_failed`."""
        try:
            judged = intake(view)
        except ControllerError as exc:
            if exc.record is None:
                self.intake_counts["intake_no_call"] += 1
                raise
            self._intake_failed(exc.record.calls)
            self._spend(exc.usage)
            if exc.usage is not None:  # #457 P1, as #452 G3: a failed call's reported tokens
                self.intake_counts["intake_tokens"] += sum(_counts(exc.usage) or ())
            raise _End("blocked", Reason("intake_failed")) from None
        except (PortInterrupted, SteerError) as exc:
            if (made := calls_made(exc)) == 0:
                if isinstance(exc, CountFailed):  # as the bound's refusal: [V2.74] (iii)
                    self.intake_counts["intake_no_call"] += 1
                raise  # #457 P2, packet §4 F3, #468: nothing went out, so nothing is counted
            self._intake_failed(made)
            self.tokens = math.nan  # abandoned or failed: billed, never reported ([V2.41] (1))
            self.usage_incomplete = True
            raise
        self.model_calls += 1
        self.intake_counts["intake_checks"] += 1
        self._spend(judged.usage)
        if judged.usage is not None:
            self.intake_counts["intake_tokens"] += sum(_counts(judged.usage) or ())
        return judged

    def _intake_failed(self, calls: int) -> None:
        self.model_calls += calls
        self.intake_counts["intake_checks"] += calls
        self.intake_counts["intake_failed"] += calls

    def _audit(self, observed: Observed, lines: tuple[ProofLine, ...]) -> None:
        """[V2.64] One, [V2.68] One, [V2.69] One and Four (audit-2): one sufficiency call. A spent
        budget: no call, `paused` with its token. No port, or a prompt over its bound: no call,
        `audit_failed`. A failed or unparsed call: `audit_failed`. Anything but sufficient with
        `facts_finish_request`: `audit_veto`. Each ends not done."""
        audit = self.ports.audit
        used = Usage(  # #404 principal 3: every budget, checked as decide checks it before a call
            actions=0,  # #452 principal 1: the audit sends no action; a task that spent its last
            # allowed action closing is audited, never paused for it (packet §4)
            model_calls=self.model_calls,  # the guard trips at `not spent < limit`, as decide
            cost_usd=self._cost(),
            wall_s=self.ports.clock() - self.started,
            tokens=self.tokens,
        )
        hit = budget_hit(self.task.budgets, used)
        if hit is not None:  # [V2.69] Four: never "done but unaudited"
            self.audit_counts["audit_budget_stops"] += 1
            raise _End("paused", Reason(hit))
        if audit is None:
            self.audit_counts["audit_no_call"] += 1
            self.audit = ("failed", AUDIT_NO_CALL)
            raise _End("blocked", Reason("audit_failed"))
        cited = {
            self.handles.by_handle[p.handle]
            for proofs in self.proofs.values()
            for p in proofs.values()
            if p.handle in self.handles.by_handle
        }
        words = words_of(span.text for s in self.subgoals for span in s.params.values())
        view = AuditView(
            observed=observed,
            typed=tuple(self.free_texts),
            handles=self.handles.bind(observed),
            proofs=lines,
            keep=self._always_keep() | cited,
            words=words,
        )
        try:
            audited = audit(view)
        except (PortInterrupted, SteerError, ControllerError) as exc:
            if isinstance(exc, PortInterrupted) and calls_made(exc) == 0:
                raise  # #468: a wait given up before the send: no call, nothing billed
            if isinstance(exc, CountFailed) or (
                isinstance(exc, ControllerError) and exc.record is None
            ):
                # refused before any call (the bound, a fence in the data, a failed count): no
                # call is counted (#468)
                self.audit_counts["audit_no_call"] += 1
                self.audit = ("failed", AUDIT_NO_CALL)
                raise _End("blocked", Reason("audit_failed")) from None
            self.model_calls += 1 if isinstance(exc, ControllerError) else calls_made(exc)
            self.audit_counts["audit_calls"] += 1
            self.audit_counts["audit_failed"] += 1
            if isinstance(exc, PortInterrupted):
                self.tokens = math.nan  # abandoned: billed, never reported ([V2.41] (1))
                self.usage_incomplete = True
                raise
            usage = exc.usage if isinstance(exc, ControllerError) else None
            self._spend(usage)
            if usage is not None:  # #452 tester G3: a failed call's reported tokens are counted
                self.audit_counts["audit_tokens"] += sum(_counts(usage) or ())
            self.audit = ("failed", AUDIT_FAILED)
            raise _End("blocked", Reason("audit_failed")) from None
        self.model_calls += 1
        self.audit_counts["audit_calls"] += 1
        self._spend(audited.usage)
        if audited.usage is not None:
            self.audit_counts["audit_tokens"] += sum(_counts(audited.usage) or ())
        if not audited.sufficient or audited.reason != "facts_finish_request":  # fail closed
            self.audit_counts["audit_vetoed"] += 1
            self.audit = ("vetoed", audited.reason)
            raise _End("blocked", Reason("audit_veto"))
        self.audit_counts["audit_allowed"] += 1
        self.audit = ("allowed", audited.reason)

    # ---- the batch ------------------------------------------------------------------------

    def _batch(
        self, actions: Sequence[ActionDoc], listed: Observed, observed: Observed
    ) -> Observed:
        """The batch, its ids read on `listed` (the observation decided on) and its first action
        re-checked on `observed`, the read taken after the decision (RT-04)."""
        self.batch_last = None
        for index, action in enumerate(actions):
            self._check_interrupt()
            acts = len(self.turn.acts)
            self.turn.at = index
            try:
                result = self._one(action, listed, observed)
            finally:
                if len(self.turn.acts) == acts:  # [V2.67] D: refused before the port
                    self.turn.acts.append(ActFact(action.verb, "not_sent"))
            if result is None:
                return observed
            after, sent = result
            observed = after
            self._auto_proofs(observed, "after_action")
            nxt = actions[index + 1] if index + 1 < len(actions) else None
            if nxt is None or not self._continues(sent, nxt, listed, observed):
                return observed
        return observed

    def _continues(
        self, sent: tuple[ActResult, EffectReport], nxt: ActionDoc, listed: Observed, now: Observed
    ) -> bool:
        act, report = sent
        if act.delivery is Delivery.DELIVERY_UNCERTAIN:
            return False
        if act.delivery is Delivery.NOT_DELIVERED and not act.action.skipped:
            return False  # a state already as wanted sent nothing, and continues
        if act.action.verb in READ_BACK_VERBS and act.read_back is not True:
            return False
        same = nxt.target is None or self._listed(nxt.target, listed, now) is not None
        return batch_continues(report, verified_type=act.read_back is True, same_target=same)

    def _one(
        self, action: ActionDoc, listed: Observed, observed: Observed
    ) -> tuple[Observed, tuple[ActResult, EffectReport]] | None:
        """Gate, dispatch and watch one action; None when it was refused (the batch ends)."""
        self.fresh = {}  # one read per element per action ([V2.29] Q2)
        element = self._target(action, listed, observed)
        if action.verb == "type" and element is not None and element.role == "ComboBox":
            self.typed_into_combobox = True  # [V2.64] Two: targeted, sent or refused
        if element is None and action.verb != "wait":
            return None
        if element is not None and self._old(observed):
            again = self._read_again(element, listed, press=action.verb == "press")
            if again is None:
                return None  # the page moved under it (`_moved`)
            observed, element = again  # everything below uses the read it is sent against
        own_text = self._verb_checks(action, element, listed, observed)
        if own_text is None:
            return None
        rated = self._risk(action, element, observed)
        if rated is None:
            return None
        risk, submit, trigger = rated
        if RISKS.index(risk) >= _R2:
            raise _End("blocked", Reason("commit_not_in_step"))
        text = self._text(action)
        digest = None if action.text is None else self.ports.fact_of(action.text).digest
        key = None if element is None else _key(action, element, text_digest=digest)
        if key is not None and not self._may_send(key, observed):
            self._refuse("repeat_send", handle=self._handle_of(element))
            return None
        if action.verb == "click" and element is not None and element.role not in ENTER_FIELDS:
            self._pre_task_draft(element, observed)
        self._check_budgets()
        sent = Dispatch(
            action=action,
            runtime_id=() if element is None else element.runtime_id,
            role="" if element is None else element.role,
            name="" if element is None else element.name,
            risk=risk,
            provenance=_provenance(action),
            submit=submit,
            trigger=trigger,
            own_text=own_text,
            text=text,
        )
        return self._dispatch(sent, element, observed, key)

    def _verb_checks(
        self, action: ActionDoc, element: Element | None, listed: Observed, observed: Observed
    ) -> bool | None:
        """The verb's own rules before the risk path. None: refused, or the world moved (the
        batch ends); else whether a Backspace goes to Steer's own text (`Dispatch.own_text`)."""
        if element is None:
            return False
        if action.verb == "type":
            refusal = type_refusal(element, observed.snapshot)
            if refusal is not None:
                self._refuse(refusal, subgoal=action.subgoal, handle=self._handle_of(element))
                return None
            return False if self._type_at_focus(element, listed, observed) else None
        if action.verb == "focus" and element.role not in FIELD_ROLES:
            # [V2.51] (1): a model fault; keyboard focus on a non-field is a step 5 question
            handle = self._handle_of(element)
            self._refuse("focus_needs_field", subgoal=action.subgoal, handle=handle)
            return None
        if action.verb == "press" and action.key == "Backspace":
            return True if self._own_text(element, observed) else None
        return False

    def _type_at_focus(self, element: Element, listed: Observed, observed: Observed) -> bool:
        """[V2.51] (3), [V2.43] (2): a type goes only where the keyboard focus is. The fault
        follows what the model saw: the listing (or this batch's verified focus) shows the
        focus elsewhere or unread, a refusal and a strike at once; shown there and gone in the
        read it is sent against, the world moved. Both are counted."""
        if self.turn.focused is not None:  # this batch's verified focus overrides the listing
            seen = self.turn.focused == element.runtime_id
        else:
            seen = type_focus_ok(focus=listed.focus, target=element, snapshot=listed.snapshot)
        handle = self._handle_of(element)
        if not seen:
            self.counts.type_unfocused += 1
            self._refuse("type_not_at_focus", handle=handle)
            return False
        if not type_focus_ok(focus=observed.focus, target=element, snapshot=observed.snapshot):
            self.counts.type_unfocused += 1
            self._moved("focus", handle=handle)
            return False
        return True

    def _own_text(self, element: Element, observed: Observed) -> bool:
        """[V2.51] (5): Backspace only on a field holding Steer's own pending text; any other
        filled field is refused (`not_own_text`) and counted."""
        if any(p.field == element.runtime_id for p in self._pending(observed)):
            return True
        self.counts.not_own_text += 1
        self._refuse("not_own_text", handle=self._handle_of(element))
        return False

    def _old(self, observed: Observed) -> bool:
        """[V2.42] RT-19: the resolving observation is older than `REUSE_FRESHNESS_S` (5 s,
        UNMEASURED; the read-cost probe measures it), from its read's completion."""
        return self.ports.clock() - observed.snapshot.taken_at > REUSE_FRESHNESS_S

    def _read_again(
        self, element: Element, listed: Observed, *, press: bool
    ) -> tuple[Observed, Element] | None:
        """[V2.42] RT-19: one fresh read (never a model call). The same element (its RuntimeId,
        held once, same role and name) and the top Document and URL unchanged since the decided-on
        observation: dispatch on it. Otherwise the staleness rule applies (`_Turn.stale_read`).
        Age alone never re-asks."""
        read = self._observe()
        found = [e for e in read.snapshot.elements if e.runtime_id == element.runtime_id]
        same = len(found) == 1 and (found[0].role, normalise_name(found[0].name)) == (
            element.role,
            normalise_name(element.name),
        )
        if same and not stale_observation(listed, read):
            if press and not press_allowed(focus=read.focus, last_acted=found[0].runtime_id):
                self._moved(
                    "focus", read, handle=self._handle_of(element)
                )  # the re-read shows focus moved: never press blind
                return None
            return read, found[0]
        self._moved("page", read)
        return None

    def _moved(self, what: Moved, read: Observed | None = None, handle: int | None = None) -> None:
        """[V2.43] (2): the world moved after the observation the model saw. With nothing
        dispatched this turn, the staleness rule applies to a fresh read (the loop re-asks, no
        refusal). After a send in the same batch, the move only ends the batch, as the batch
        rule does: it is counted with its token and the turn is scored normally, so its send is
        never forgotten (principal re-review: staleness applies when nothing was dispatched)."""
        self.turn.moved_handle = None if what == "page" else handle
        if self.turn.sent > 0:
            self._count_moved(what)
            self.turn.moved_after_send = True
            return
        self.turn.stale_read = self._observe() if read is None else read
        self.turn.moved = what

    def _count_moved(self, what: Moved) -> None:
        """Each kind of world move counted on its own, with its history token."""
        if what == "page":
            self.stale_count += 1
        elif what == "target":
            self.target_changes += 1
        else:
            self.focus_losses += 1
        self._note(WorldMoved(what, self.turn.moved_handle))  # [V2.47]: its element

    def _text(self, action: ActionDoc) -> str | None:
        """RT-36: the text a `type` sends: its param's goal span, or its free text."""
        if action.verb != "type":
            return None
        if action.param is None:
            return action.text
        return self._subgoal(action.subgoal).params[action.param].text

    def _may_send(self, key: ActionKey, observed: Observed) -> bool:
        """[V2.18]: the same send again only after progress, against the previous turn's last
        send and, within this batch, the last one sent (RT-07)."""
        present = frozenset(e.runtime_id for e in observed.snapshot.elements)
        if not self.strikes.may_send(key, present=present):
            return False
        if self.batch_last is None:
            return True
        earlier, effect = self.batch_last
        return not same_action(earlier, key, present) or made_progress(key.verb, effect)

    def _target(self, action: ActionDoc, listed: Observed, observed: Observed) -> Element | None:
        """§8.1: the element the action names, re-resolved; a press goes to the last acted
        element only when the keyboard focus is read there ([V2.6]) and it still holds the same
        role and name (P4)."""
        if action.verb == "wait":
            return None
        if action.verb == "press":
            return self._press_target(listed, observed)
        if action.target is None:  # validate refuses this shape first
            raise AssertionError("target_missing")
        element = self._listed(action.target, listed, observed)
        if element is None and self.handles.element(action.target, listed) is not None:
            self._moved("target", handle=action.target)  # RT-27, [V2.43] (2): listed, then moved
        elif element is None:
            self._refuse("id_not_listed")
        return element

    def _listed(self, handle: int, listed: Observed, now: Observed) -> Element | None:
        """§8.1, [V2.38] RT-17: the element the handle names, only when its RuntimeId is in the
        listing the decision saw and still in `now` with the same role and name."""
        if listed.snapshot.hwnd != now.snapshot.hwnd:
            return None  # another window: nothing listed there (RT-13)
        then = self.handles.element(handle, listed)
        element = self.handles.element(handle, now)
        if then is None or element is None:
            return None
        same = (element.role, normalise_name(element.name)) == (
            then.role,
            normalise_name(then.name),
        )
        return element if same else None

    def _press_target(self, listed: Observed, observed: Observed) -> Element | None:
        """[V2.6], [V2.43] (2): the fault follows what the model could see. Focus elsewhere or
        unread in the listing it saw is its fault (a refusal and a strike); focus on the target
        there and gone now means the world moved (re-observe, re-ask)."""
        acted = self.last_acted
        last = None if acted is None else acted.runtime_id
        handle = None if not last else self.handles.by_rid.get(last)
        if not press_allowed(focus=listed.focus, last_acted=last):
            self._refuse("press_not_at_focus", handle=handle)
            return None
        if not press_allowed(focus=observed.focus, last_acted=last):
            self._moved("focus", handle=handle)
            return None
        element = next((e for e in observed.snapshot.elements if e.runtime_id == last), None)
        if element is None or acted is None:
            self._refuse("press_target_gone", handle=handle)
            return None
        if (element.role, element.name) != (acted.role, acted.name):
            self._refuse("press_target_changed", handle=handle)
            return None
        return element

    def _risk(
        self, action: ActionDoc, element: Element | None, observed: Observed
    ) -> tuple[Risk, bool, bool] | None:
        """The action's risk, and whether it is a submit that goes through the Enter table
        ([V2.28] item 1: an Enter on a field, or a click or key that submits pending text)."""
        if element is None:  # a wait sends nothing; validate refuses a commit on it (RT-42)
            risk = COMMIT_RISK[action.commit]
            self.turn.ratings[self.turn.at] = (risk, None if risk == "R0" else "commit_declared")
            return risk, False, False
        verdict = action_risk(
            action, element, goal=self.task.goal, observed=observed, pending=self._pending(observed)
        )
        self.turn.ratings[self.turn.at] = (verdict.risk, verdict.why)  # [V2.67] D
        if verdict.submit_parent is not None:  # [V2.25]: counted where it fires, once an action
            self.submit_parents[verdict.submit_parent] += 1
        if verdict.refusal is not None:
            self._refuse(verdict.refusal, subgoal=action.subgoal, handle=self._handle_of(element))
            return None
        return verdict.risk, verdict.submit, verdict.trigger  # #350: one classification

    def _pending(self, observed: Observed) -> list[PendingText]:
        """[V2.37] P2, [V2.38] RT-03: every field typed into in this task that may still hold the
        text, found by RuntimeId or else by its ledger identity under the same top Document.
        Several identity matches keep it pending, read as unread. A replaced top Document, a
        field proven gone (no match of either kind on a stable observation) or emptied ends it."""
        top = top_document(observed.snapshot)
        scope = None if top is None else top.runtime_id
        kept: list[_Typed] = []
        out: list[PendingText] = []
        found: list[_Pending] = []
        for typed in self.typed:
            if typed.scope is not None and scope is not None and typed.scope != scope:
                continue  # a replaced top Document ends it; an unreadable one proves nothing
            matches = _matches(typed, observed)
            if not matches:
                if not _complete(observed):
                    kept.append(typed)  # gone is not proven on a read that may be partial
                    found.append(_Pending(typed, None, None))
                continue
            field = matches[0]
            now = None if len(matches) > 1 else self._read_value(field, observed)
            if now is not None and not now.filled:
                continue  # emptied: the text is no longer pending
            kept.append(typed)
            found.append(_Pending(typed, field if len(matches) == 1 else None, now))
            out.append(
                PendingText(field.runtime_id, typed.typed, now, typed.search, typed.goal_span)
            )
        self.typed = kept
        self.pending = found
        return out

    def _pre_task_draft(self, target: Element, observed: Observed) -> None:
        """[V2.64] Two, [V2.37]'s recorded limit: count the click once when a field in its scope
        holds text and has no pending-text record. A count only, never a refusal; secure fields
        are never read, and the reads stop at the first filled one."""
        mine = {m.runtime_id for typed in self.typed for m in _matches(typed, observed)}
        for held in scope_fields(observed, target):
            if held.runtime_id in mine:
                continue
            value = self._read_value(held, observed)
            if value is not None and value.filled:
                self.pre_task_draft_clicks += 1
                return

    def _readers(self, observed: Observed) -> Readers:
        """[V2.29] Q2: the claim checks' live reads, bound to the ports, taken at the claim and
        never served from an earlier read (#331 review); a secure or secure-unread field is
        never read."""

        def read_value(element: Element) -> ValueFact | None:
            if element.secure or element.id in observed.snapshot.unread_secure:
                return None
            return self.ports.read_value(element)

        return Readers(
            read_state=self.ports.read_state, read_value=read_value, fact_of=self.ports.fact_of
        )

    def _typed_fields(self) -> tuple[tuple[int, ...], ...]:
        """[V2.38] RT-39: the echo guards are task-wide: every field Steer typed into in the
        task, whether its text is still pending or not."""
        return tuple(dict.fromkeys(e.field for e in self.ever))

    def _acted(self) -> tuple[int, ...] | None:
        """[V2.73]: the element whose outermost Document scopes the carriers (the [V2.3]
        observer scope rule): the most recently acted element in the task, whatever subgoal its
        action was labelled with; None when nothing was acted on (the observer's own rule)."""
        return None if self.last_acted is None else self.last_acted.runtime_id

    def _log_entry(self, sent: Dispatch, act: ActResult, report: EffectReport) -> LogEntry:
        """[V2.46], §8.6: the action as closed tokens and its task handle; never the target's
        name and never the typed text (a type names its param)."""
        action = sent.action
        read = {True: None, False: "mismatch", None: None}[act.read_back]
        if action.verb in READ_BACK_VERBS and act.read_back is None:
            read = "unread"
        return LogEntry(
            verb=action.verb,
            handle=self.handles.by_rid.get(sent.runtime_id) if sent.runtime_id else None,
            role=log_role(sent.role) if sent.runtime_id else "unknown",
            subgoal=action.subgoal,
            param=action.param if action.verb == "type" else None,
            delivery=act.delivery.value,
            classes=tuple(sorted(report.classes)),
            tokens=tuple(sorted(report.tokens)),
            read_back=read,
        )

    def _echo(self, subgoal: Subgoal) -> bool:
        """[V2.38] RT-39: the subgoal's words are text Steer typed anywhere in the task; any
        generated text counts, since its words are not kept."""
        words = {_folded(span.text) for span in subgoal.params.values()}
        return any(e.words is None or _folded(e.words) in words for e in self.ever)

    def _read_value(self, element: Element, observed: Observed) -> ValueFact | None:
        """[V2.29] Q2: never read a field whose secure flag is set or unread; one read per
        element per action."""
        if element.secure or element.id in observed.snapshot.unread_secure:
            return None
        if element.runtime_id not in self.fresh:
            self.fresh[element.runtime_id] = self.ports.read_value(element)
        return self.fresh[element.runtime_id]

    def _dispatch(
        self,
        sent: Dispatch,
        element: Element | None,
        observed: Observed,
        key: ActionKey | None,
    ) -> tuple[Observed, tuple[ActResult, EffectReport]] | None:
        before = observed
        if element is not None:  # [V2.28] item 3: the target's own value, just before dispatch
            before = _with_value(observed, self._read_value(element, observed))
        act = self._act(sent)
        detail = act.action.evidence.get("detail")
        if isinstance(detail, str) and detail in TAKEOVER_TOKENS:
            # [V2.57] LW-05, [V2.58] (2): a person, or another Steer, before or during the input
            raise _End("stopped", Reason(detail))
        if act.delivery is Delivery.NOT_DELIVERED and not act.action.skipped:
            self._not_delivered(act, element)
            return None
        self._count_toggles(act)
        if sent.action.verb == "focus" and act.read_back is True and element is not None:
            self.turn.focused = element.runtime_id
        pending_before = list(self.pending)  # each pending field as found just before dispatch
        watch_started = self.ports.clock()
        watched = self.ports.watch(before, sent)
        self.turn.watches.append(WatchFacts(watch_kind(sent), self.ports.clock() - watch_started))
        # [V2.48]: counted only once the watch returned; one that raised (a kill, a port error)
        # never reached a deadline verdict and is in neither count
        counted = self.watch_counts[watch_kind(sent)]
        counted[0] += 1
        counted[1] += watched.timed_out
        self.read_costs.append(watched.read_cost)
        self.turn.sent += 1
        report = watched.report
        self.turn.action, self.turn.effect = key, report
        if key is not None:
            self.batch_last = (key, report)
        if element is not None:
            self.last_acted = _Acted(element.runtime_id, element.role, element.name)
        self._note(self._log_entry(sent, act, report))
        if element is not None and _focus_left(sent, element, watched.after):
            self.counts.focus_moved_after_press += 1  # [V2.43] (1): seen and counted, no extra read
            self._note(FocusMovedAfterPress(self._handle_of(element)))
        if not report.classes <= {"none", "unknown"}:
            self.refusal = None  # [V2.12]: progress resets the refusal that decides the status
        if sent.submit:
            self._judge_submit(element, before=observed, watched=watched, pending=pending_before)
        if element is not None and sent.action.verb == "type":
            self._remember_type(sent, element, observed, verified=act.read_back is True)
        return self._after(watched.after), (act, report)

    def _count_toggles(self, act: ActResult) -> None:
        """[V2.52] (a): a second Toggle that fired, one refused, and one the re-read made
        unneeded (the wanted state arrived late), each counted on its own."""
        evidence = act.action.evidence
        if evidence.get("toggles") == 2:
            self.counts.second_toggles += 1
        elif evidence.get("detail") == "second_toggle_refused":
            self.counts.second_toggles_refused += 1
        elif evidence.get("detail") == "settled_on_reread":
            self.counts.toggles_settled_on_reread += 1

    def _not_delivered(self, act: ActResult, element: Element | None) -> None:
        """Nothing was sent. The act port's takeover (the person kept using the machine) stops
        the task as the switch's would; its wall cap is the wall budget; a field that already
        held text goes back to the controller as its own token ([V2.51] (5)), counted."""
        detail = act.action.evidence.get("detail")
        if isinstance(detail, str) and detail in TAKEOVER_TOKENS:
            raise _End("stopped", Reason(detail))
        if detail == "wall_budget":
            self._check_budgets()
        if detail == "field_not_empty":
            self.counts.fields_not_empty += 1
            self._refuse("field_not_empty", handle=self._handle_of(element), detail=detail)
            return
        self._refuse("not_delivered", handle=self._handle_of(element), detail=detail, cause=detail)

    def _judge_submit(
        self,
        element: Element | None,
        *,
        before: Observed,
        watched: Watched,
        pending: Sequence[_Pending],
    ) -> None:
        """[V2.28] item 1: the Enter table once, on the final report. `unexpected_effect` stops
        the task; any other row is a closed token for the controller. A report the watch did not
        hold (neither conclusive nor to the guard) is not final (#335 review): the stops of rows
        1, 2, 3 and 3b rest on facts that do not depend on timing and stand; row 4 depends on
        timing, reached only after the hold ([V2.28]), so it and every row that does not stop
        are `unknown`, row 5."""
        outcome = enter_outcome(
            watched.report,
            before=before,
            after=watched.after,
            typed_field=self._submitted_field(element, watched.after, pending),
        )
        held = watched.timed_out or watched.conclusive_ms is not None
        if not held and (outcome.outcome != "unexpected_effect" or outcome.row == "4"):
            outcome = EnterOutcome("back_to_controller", "unknown", "5")
        self._note(Entered(outcome.token, outcome.row))
        if outcome.outcome == "unexpected_effect":
            raise _End("stopped", Reason("unexpected_effect"))

    def _submitted_field(
        self, element: Element | None, after: Observed, pending: Sequence[_Pending]
    ) -> FieldState:
        """Row 2's fact: the typed field the submit sent, read again on the final report. The
        Enter's own field when it was pending; else every pending field (the worst state wins:
        emptied, then unread, then gone, then kept). Each is found again by its `_Typed` record,
        so a field re-rendered with the same identity is still the field (#335 review, RT-03)."""
        mine = [
            p
            for p in pending
            if element is not None
            and p.element is not None
            and p.element.runtime_id == element.runtime_id
        ]
        fields = mine or list(pending)
        if not fields:
            return "not_typed"
        self.fresh = {}
        states = [self._field_now(p, after) for p in fields]
        order: tuple[FieldState, ...] = ("emptied", "unread", "gone", "kept")
        return next(worst for worst in order if worst in states)

    def _field_now(self, pending: _Pending, after: Observed) -> FieldState:
        """One pending field on the final report. Unmatched or ambiguous before the submit, or
        ambiguous now, it is `unread`: row 2 then gives `unknown`, never a guess."""
        if pending.element is None:
            return "unread"
        matches = _matches(pending.typed, after)
        if len(matches) > 1:
            return "unread"
        now = matches[0] if matches else None
        read = None if now is None else self._read_value(now, after)
        return field_state(pending.value, read, present=now is not None)

    def _mark_acted(self, target: int | None) -> None:
        """#366 P5: the dispatched target's handle on the turn whose Decision named it."""
        turns = self.listing.turns
        if target is None or not turns:
            return
        turns[-1] = replace(turns[-1], acted=turns[-1].acted | {target})

    def _act(self, sent: Dispatch) -> ActResult:
        """[V2.25] (c): one local retry when nothing was sent, under the same decision, unless
        the person stopped the task in between (RT-15)."""
        self.dispatched.append(sent)  # before the call: an act that raises may still have landed
        self._mark_acted(sent.action.target)
        act = self.ports.act(sent)
        if may_retry(risk=sent.risk, verified=False, report=None, act=act.action):
            self._check_interrupt()
            act = self.ports.act(sent)
        result = "skipped" if act.action.skipped else act.delivery.value
        self.turn.acts.append(ActFact(sent.action.verb, result))  # [V2.67] D
        return act

    def _remember_type(
        self, sent: Dispatch, element: Element, observed: Observed, *, verified: bool
    ) -> None:
        """P1/RT-02: every type that may have sent text is pending; its typed value is read only
        after a verified type, else unread (never a query)."""
        self.fresh.pop(element.runtime_id, None)  # the value after the type, not before it
        top = top_document(observed.snapshot)
        typed = _Typed(
            field=element.runtime_id,
            identity=_identity(element),
            scope=None if top is None else top.runtime_id,
            typed=self.ports.fact_of(sent.text) if verified and sent.text else None,
            search=search_facts(observed, element.runtime_id),
            goal_span=sent.provenance == "goal_span",
        )
        self.typed = [t for t in self.typed if t.field != typed.field]
        self.typed.append(typed)
        param = sent.action.param
        subgoal = self._subgoal(sent.action.subgoal)
        words = (
            subgoal.params[param].text if param is not None and param in subgoal.params else None
        )
        self.ever.append(_Ever(element.runtime_id, words))
        if param is None and sent.text:  # packet rev 5 item 5: masked out of the audit's view
            self.free_texts.append(sent.text)

    def _after(self, after: Observed) -> Observed:
        """[V2.11] decision 6: reuse the watch's after only when it is full, stable and fresh."""
        after = self._in_domain(after)  # stamped like every observation (RT-11)
        age = self.ports.clock() - after.snapshot.taken_at
        if after.tier == "full" and after.snapshot.stable and age <= REUSE_FRESHNESS_S:
            return after
        return self._observe()

    def _judge_turn_start(self, observed: Observed) -> None:
        """[V2.67] A: the read a decision is about to be made on is judged first; when that
        closes the task, the model call this turn would have made is not made and is counted."""
        proven = len(self.proven)
        read, self.next_read = self.next_read, "turn_start"
        try:
            self._auto_proofs(observed, read)
        except _End as end:
            if end.status == "done" and len(self.proven) > proven:
                self.saved_calls += 1
            raise

    def _auto_proofs(self, observed: Observed, read_kind: ProofRead) -> None:
        """§7.1, [V2.67] A: the code-only proof runs on every full observation code takes (the
        turn-start read, the stale re-read and the after-action read), for each open subgoal
        with a change signal since its baseline: same rules, guards and baseline, every param on
        this one read ([V2.39]). Only a stable full-tier read is judged, never a poll (a page
        still growing is: its evidence is there). The same read is judged once. [V2.67] D: every
        attempt, a skipped read and a closed gate included, goes to the turn's trace record with
        every condition it rests on."""
        if observed is self.judged:
            return
        self.judged = observed
        tracing = self.ports.turn_trace is not None  # #444: no trace work without a sink
        skipped: Skipped | None = None
        if observed.tier != "full":
            skipped = "poll"
        elif not observed.snapshot.stable:
            skipped = "unstable"
        for position, subgoal in enumerate(self.subgoals):  # the trace's 0-based positions
            if subgoal.id in self.proven:
                continue
            if subgoal.closes != "page":
                continue  # [V2.39]: the navigated auto proof closes only a page subgoal
            if subgoal.commit != "none":
                continue  # #474: a commit subgoal closes only in the strict tier (step 4)
            baseline = self._baseline(subgoal.id)  # [V2.72]: every open page subgoal is judged
            gate = None if skipped is not None else _change_gate(baseline, observed)
            if gate is None:
                if tracing:
                    self.turn.proofs.append(
                        _attempt(position, read_kind, observed, skipped, gate, None)
                    )
                continue
            judged = judge(
                subgoal,
                baseline,
                observed,
                typed=self._typed_fields(),
                acted=self._acted(),
                echo=self._echo(subgoal),
                allow_file=self._allow_file(),
            )
            if tracing:
                self.turn.proofs.append(_attempt(position, read_kind, observed, None, gate, judged))
            if judged.proof is not None:
                self._tick(subgoal, how=judged.proof, at=observed)
        if skipped is None:
            self._check_done(observed)

    # ---- refusals, guards -----------------------------------------------------------------

    def _refuse(
        self,
        *tokens: RefusalWord,
        subgoal: str | None = None,
        handle: int | None = None,
        detail: object = None,
        cause: object = None,
    ) -> None:
        """[V2.11] decision 4, [V2.37] P5: nothing dispatched is a refusal with a token and a
        strike; it replaces the latest refusal, whatever its kind. [V2.42] RT-29: several tokens
        take the kind by priority and every token is recorded. RT-30: a policy or step-limit
        refusal also attaches to the refused action's subgoal and stands until it is proven.
        [V2.85] (iv): the history shows `cause`, the act's own closed detail, in place of the
        broader `not_delivered` when code knows one; the kind and the counts stay the token's."""
        kind, token = refusal_of(tokens)
        self.refusal = (kind, token)
        shown = detail_token(detail)  # [V2.67] D: every token, closed, with its detail
        self.turn.refusals.extend(RefusalFact(word, shown) for word in tokens)
        for word in tokens:  # every token is counted, as every token is recorded (RT-29)
            self.refusal_counts[word] = self.refusal_counts.get(word, 0) + 1
        specific = cause if is_cause(cause) else None
        self._notes(  # [V2.47]: its element
            Refused(specific if specific is not None and word == "not_delivered" else word, handle)
            for word in tokens
        )
        self.turn.refused = True
        if kind in ("policy", "step") and subgoal is not None:
            self.refused_seq += 1
            self.standing.setdefault(subgoal, []).append((kind, token, self.refused_seq))

    def _guard_left(self, observed: Observed) -> float:
        """[V2.38] RT-24: what is left of the one load guard of this observation's top Document,
        capped by the wall budget left; 0 when it has no Document time. A future or broken stamp
        reads as a Document seen just now (T6, RT-20): the guard never exceeds `LOAD_GUARD_S`."""
        since = observed.document_since
        if since is None:
            return 0.0
        now = self.ports.clock()
        age = now - since
        age = age if math.isfinite(age) and age > 0 else 0.0
        wall_left = self.task.budgets.max_wall_s - (now - self.started)
        return max(0.0, min(LOAD_GUARD_S - age, wall_left))

    def _hold(self, observed: Observed) -> Observed | None:
        """[V2.31], [V2.38] RT-12: the load guard. Before the task ends on a `cannot` stop or on
        the strike limit while the top Document's guard is not spent, wait (the `settle` port)
        until its node count holds or the rest of the guard expires, then re-observe and re-ask:
        once per top Document. None when the guard does not apply. The person's kill is honoured
        right after the wait (RT-23)."""
        top = top_document(observed.snapshot)
        left = self._guard_left(observed)
        if top is None or top.runtime_id in self.reasked or left <= 0:
            return None
        self.reasked.add(top.runtime_id)
        self._note(LoadGuard())
        held = self.ports.settle(observed, left)
        self._check_interrupt()
        return self._in_domain(held)

    def _stale(self, fresh: Observed, *, moved: Moved) -> Observed:
        """[V2.28] item 1, [V2.43] (2): the world moved after the observation the model saw (the
        page, the named target, or the focus before a press); the Decision is discarded and the
        controller asked again on the fresh read. No strike for one; the second in a row (and
        each after) is a strike. Each kind is counted on its own."""
        self.stale_streak += 1
        self.next_read = "stale_reread"  # [V2.67] D: the next turn starts on this re-read
        self._count_moved(moved)
        if self.stale_streak < 2:
            self.turn.neutral = True
        else:
            self.turn.refused = True
        return self._record_strike(fresh)

    def _trace_turn(self) -> None:
        """[V2.67] D: the turn's closed record to the trace port; a record that fails the
        no-free-text check is dropped and counted, never written in part. #444: an exception
        while building or writing it is never raised: the turn is often the one the task ends in
        (`_outcome`), and its outcome must never be lost. #463 (1): it is counted as a trace
        error, apart from the drops; an interrupt is not an `Exception` and propagates."""
        sink = self.ports.turn_trace
        turn = self.turn
        if sink is None or turn.seen is None or turn.traced:
            return
        turn.traced = True
        try:
            self._write_turn(sink, turn)
        except Exception:  # noqa: BLE001 - a trace failure never loses the task; it is counted
            self.turn_trace_errors += 1

    def _write_turn(self, sink: Callable[[Mapping[str, object]], None], turn: _Turn) -> None:
        seen = turn.seen
        if seen is None:  # checked by the caller; kept for the type checker
            return
        listing = turn.listing
        record = turn_record(
            TurnTrace(
                turn=turn.number,
                listed=None if listing is None else listing.shown,
                cut=None if listing is None else listing.cut,
                growing=seen.growing,
                young=seen.young,
                focus=focus_state(seen.snapshot, seen.focus),
                decision=_rated(turn.shape, turn.ratings),
                refusals=tuple(turn.refusals),
                acts=tuple(turn.acts),
                repair=turn.repair,
                repair_tokens=turn.repair_tokens,
                watch=tuple(turn.watches),
                tokens=turn.usage,
                history=tuple(turn.history),
                proofs=tuple(turn.proofs),
            )
        )
        if closed(record):
            sink(record)
        else:
            self.turn_trace_dropped += 1

    def _record_strike(self, observed: Observed) -> Observed:
        if self.turn.neutral:
            return observed
        facts = TurnFacts(
            ticked=self.turn.ticked,
            action=self.turn.action,
            effect=self.turn.effect,
            state_digest=_digest(observed),
            claim_rejected=self.turn.claim_rejected,
            present=frozenset(e.runtime_id for e in observed.snapshot.elements),
            refused=self.turn.refused and self.turn.sent == 0,  # RT-25/26: nothing dispatched
        )
        reasons = self.strikes.record(facts)
        if reasons:
            self.strike_log.append(reasons)
            self.strike_words.append(frozenset(f.refusal for f in self.turn.refusals))
        if self.strikes.paused:
            held = self._hold(observed)
            if held is None:  # [V2.69] Two: one cause behind every strike is named
                cause = single_cause(zip(self.strike_log, self.strike_words, strict=True))
                raise _End("paused", Reason("no_progress", detail=cause))
            return held
        return observed

    def _check_budgets(self) -> None:
        """Invariant 9 ([V2.37] P6): actions, calls, tokens, wall time, and cost when priced."""
        used = Usage(
            actions=len(self.dispatched),
            model_calls=self.model_calls,
            cost_usd=self._cost(),
            wall_s=self.ports.clock() - self.started,
            tokens=self.tokens,
        )
        hit = budget_hit(self.task.budgets, used)
        if hit is not None:
            raise _End("paused", Reason(hit))

    def _check_interrupt(self) -> None:
        token = self.ports.interrupted()
        if token is None:
            return
        try:
            cause = Reason(token)
        except ValueError:  # RT-37: a malformed token still stops the task
            cause = Reason("interrupted")
        raise _End("stopped", cause)


# ---- pure helpers -----------------------------------------------------------------------------


def _query_combobox_listed(
    observed: Observed, handles: Mapping[int, int], shown: frozenset[int] | None
) -> bool:
    """[V2.64] Two: a ComboBox among the rows shown (every bound handle when `shown` is None)
    whose AriaRole is `searchbox` or that sits under a Search landmark."""
    roles = observed.aria_roles or {}
    for element in observed.snapshot.elements:
        if element.role != "ComboBox" or element.id not in handles:
            continue
        if shown is not None and handles[element.id] not in shown:
            continue
        rid = element.runtime_id
        if roles.get(rid) == "searchbox" or search_landmark_of(observed, rid) is not None:
            return True
    return False


def _no_page(observed: Observed) -> bool:
    """[V2.44] (2), the [V2.31] shapes: a browser window (it has an address bar) with no
    Document at all, or a top Document with nothing in it. A native window is never this, nor
    a window with several root Documents (unreadable, not blank: the model sees it)."""
    elements = observed.snapshot.elements
    if not any(e.role == "Document" for e in elements):
        return observed.url_observed
    inside = document_nodes(observed.snapshot)
    return inside is not None and not inside


def _window_gone(observed: Observed) -> bool:
    """The task's window is no longer among the top-level windows (infrastructure)."""
    windows = observed.windows
    return windows is not None and observed.snapshot.hwnd not in windows


def refusal_of(tokens: Sequence[str]) -> tuple[RefusalKind, str]:
    """[V2.42] RT-29: the kind of a refusal with these tokens, by a fixed priority (policy, step
    limit, world, model fault), never by their order; the token is the first of that kind."""
    kinds = [(REFUSAL_KIND[t], t) for t in tokens]
    for kind in KIND_ORDER:
        for found, token in kinds:
            if found == kind:
                return kind, token
    raise ValueError("a refusal needs a token")


def _rated(
    shape: Mapping[str, Any] | None, ratings: Mapping[int, tuple[Risk, RiskWhy | None]]
) -> dict[str, Any] | None:  # Any: JSON values (`decision_shape`)
    """[V2.67] D: the decision's shape with each action's rated tier and the rule that set it;
    None for an action the batch never rated (it ended first, or it was refused before)."""
    if shape is None:
        return None
    actions = [
        {**a, "risk": ratings.get(i, (None, None))[0], "why": ratings.get(i, (None, None))[1]}
        for i, a in enumerate(shape["actions"])
    ]
    return {**shape, "actions": actions}


def _step_subgoal(decision: Decision) -> str | None:
    """[V2.42] RT-30: the subgoal a step-limit refusal of this Decision belongs to: the first
    action whose verb step 3 refuses; None when no action names one."""
    return next((a.subgoal for a in decision.actions if a.verb in NOT_IN_STEP_VERBS), None)


def _counts(usage: TokenUsage | None) -> tuple[int, int, int, int, int] | None:
    """[V2.37] P6, [V2.40] Q3, [V2.45]: a call's token counts by category; None when the usage
    is missing, a count is not a whole number of tokens, zero or more, or the call had no input
    at all (every call sends input, fresh or from the cache)."""
    if usage is None:
        return None
    counts = (
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_write_5m,
        usage.cache_write_1h,
        usage.cache_read,
    )
    if any(isinstance(c, bool) or not isinstance(c, int) or c < 0 for c in counts):
        return None
    fresh, _, write_5m, write_1h, read = counts
    if fresh + write_5m + write_1h + read == 0:
        return None
    return counts


def _shared_rids(observed: Observed) -> frozenset[tuple[int, ...]]:
    """The RuntimeIds more than one element of the snapshot holds."""
    seen: set[tuple[int, ...]] = set()
    shared: set[tuple[int, ...]] = set()
    for element in observed.snapshot.elements:
        rid = element.runtime_id
        if rid:
            (shared if rid in seen else seen).add(rid)
    return frozenset(shared)


def _folded(text: str) -> str:
    """Words as the echo guard compares them: NFKC, collapsed whitespace, case-folded."""
    return normalise_name(text).casefold()


def spans_shared(a: GoalSpan, b: GoalSpan) -> bool:
    """[V2.72] Two, #473 review: two params share a span when their ranges of the request
    overlap (one contains the other included) or their words are the same, folded (the same
    words at another place in the request)."""
    overlap = a.start < b.end and b.start < a.end
    return overlap or _folded(a.text) == _folded(b.text)


def _shares_span(one: Subgoal, other: Subgoal) -> bool:
    return any(spans_shared(a, b) for a in one.params.values() for b in other.params.values())


def _proved_param(claim: Claim, subgoal: Subgoal) -> str | None:
    """[V2.39] (2): which param a value or state claim proves. The claim's param is only a hint
    for that; with none, a subgoal with exactly one param takes it."""
    if claim.param is not None:
        return claim.param if claim.param in subgoal.params else None
    names = list(subgoal.params)
    return names[0] if len(names) == 1 else None


def _complete(observed: Observed) -> bool:
    """[V2.38] RT-03: a read that can prove a field gone: stable, full tier, and the page neither
    growing nor young (more may still arrive)."""
    settled = observed.snapshot.stable and observed.tier == "full"
    return settled and not observed.growing and not observed.young


def _identity(element: Element) -> Identity:
    return (element.role, normalise_name(element.name), element.automation_id)


def _matches(typed: _Typed, observed: Observed) -> list[Element]:
    """The field by its RuntimeId, else every element with its ledger identity ([V2.38] RT-03)."""
    elements = observed.snapshot.elements
    by_rid = [e for e in elements if e.runtime_id == typed.field]
    if by_rid:
        return by_rid
    return [e for e in elements if e.runtime_id and _identity(e) == typed.identity]


def _focus_left(sent: Dispatch, element: Element, after: Observed) -> bool:
    """[V2.43] (1): a Space or an Enter on a state-set element, and the watch's after-read shows
    the focus elsewhere. An arrow is exempt (it moves focus by design); an unread focus is never
    counted as moved."""
    action = sent.action
    pressed = action.verb == "press" and action.key in {"Space", "Enter"}
    if not pressed or element.role not in STATE_SET_VERB or after.focus is None:
        return False
    return after.focus != element.runtime_id


def _with_value(observed: Observed, value: ValueFact | None) -> Observed:
    return replace(observed, acted_value=value)


def _span(goal: str, text: str) -> GoalSpan:
    """A param's text as a span of the goal; `validate` already checked it is one."""
    start = goal.index(text)
    return GoalSpan.of(goal, start, start + len(text))


def _provenance(action: ActionDoc) -> Provenance | None:
    """[V2.11] decision 5: set by code; free text is always generated."""
    if action.verb != "type":
        return None
    return "goal_span" if action.param is not None else "generated"


def _key(action: ActionDoc, element: Element, *, text_digest: str | None) -> ActionKey:
    """[V2.11] decision 3: the action's own closed fields; a free-text type adds its text's
    digest (RT-34), never the text."""
    fields = (action.key, action.param, action.option, text_digest)
    params = tuple(p for p in fields if p is not None)
    return ActionKey(
        verb=action.verb, params=params, runtime_id=element.runtime_id, identity=_identity(element)
    )


def _change_gate(baseline: Observed, now: Observed) -> Gate | None:
    """§7.1's change signal, and which: the URL changed (both read), or the top Document was
    replaced; None: neither, so the proof is not tried ([V2.67] D names it)."""
    if baseline.url is not None and now.url is not None and baseline.url != now.url:
        return "url"
    old, new = top_document(baseline.snapshot), top_document(now.snapshot)
    if old is not None and new is not None and old.runtime_id != new.runtime_id:
        return "document"
    return None


def _digest(observed: Observed) -> str:
    """The element-set digest for `state_seen` (in memory only, never logged): identity and the
    pattern states, so ticking another box is new state (RT-09)."""
    rows = sorted((e.runtime_id, e.role, e.name, e.caps) for e in observed.snapshot.elements)
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def _attempt(  # noqa: PLR0913, PLR0917 - one attempt's facts, positional for brevity
    position: int,
    read_kind: ProofRead,
    observed: Observed,
    skipped: Skipped | None,
    gate: Gate | None,
    judged: Judged | None,
) -> ProofAttempt:
    """[V2.67] D: one judgement of one subgoal on one read as closed facts: positions, closed
    tokens and booleans, never a name, a word or an address."""
    read = ReadFacts(
        tier=observed.tier,
        stable=observed.snapshot.stable,
        growing=observed.growing,
        young=observed.young,
    )
    if judged is None:
        return ProofAttempt(subgoal=position, read_kind=read_kind, read=read, skipped=skipped,
                            gate=gate, change=False, change_by=(), refused=(), params=(),
                            content=False, content_by=None, guards=(), proven=False,
                            proven_how=None)  # fmt: skip
    params = tuple(
        ParamProof(
            param=n,
            carriers=tuple(
                CarrierFact(carrier=c, held=held, absent_before=None if was is None else not was)
                for c, held, was in facts
            ),
        )
        for n, (_name, facts) in enumerate(judged.params)  # sorted by name, as decision_shape
    )
    return ProofAttempt(
        subgoal=position,
        read_kind=read_kind,
        read=read,
        skipped=skipped,
        gate=gate,
        change=bool(judged.change_by),
        change_by=judged.change_by,
        refused=judged.refused,
        params=params,
        content=judged.content_by is not None,
        content_by=judged.content_by,
        guards=judged.guards,
        proven=judged.proof is not None,
        proven_how=judged.proof,
    )


def _read_token(subgoal: Subgoal, how: str) -> tuple[ReadKind, str]:
    """[V2.68] One: what code verified for a proven subgoal, as a closed (kind, detail) pair that
    the audit's prompt renders (`kind:detail`); no string is built here (#347 decision B)."""
    if subgoal.closes == "value":
        return ("value_read", "equals_param")
    if subgoal.closes == "state":
        if subgoal.wanted is None:  # `validate` refused a state subgoal without one
            raise AssertionError("state subgoal without wanted")
        return ("state_read", subgoal.wanted)
    return ("navigated", how)


def numbered_params[V](params: Mapping[str, V]) -> tuple[tuple[int, V], ...]:
    """#457 review, coordinator ruling: the one numbering both judges read: a subgoal's params
    by their names' sorted order, 1-based (`param:1`), each with its value. The model's names
    are never shown, only their order."""
    return tuple((n, params[name]) for n, name in enumerate(sorted(params), 1))


def _intake_lines(subs: Sequence[SubgoalDoc]) -> tuple[IntakeLine, ...]:
    """[V2.68] One, packet rev 5 (P1): each declared subgoal by position and closing kind, its
    params by sorted position with their words; never the model's id, param names or desc."""
    return tuple(
        (
            position,
            doc.closes,
            numbered_params(doc.params),
            doc.wanted,
            doc.field,
        )
        for position, doc in enumerate(subs, 1)
    )
