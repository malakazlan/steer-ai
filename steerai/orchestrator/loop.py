"""The core loop (design 4, 5.11, [v1.2]): lookup → decide → gate → act → verify → learn, one
step at a time, with nothing irreversible outside the ledger's record.

`run_step`: a stable before snapshot (an R0/R1 step may reuse the snapshot the previous verdict
was decided on while it is fresh; an R2/R3 step always observes fresh), the target resolved by
exact role and name (the chooser plugs in here later; twins are ambiguity, not a pick), the
ledger's admission (done already, fresh, or confirm after an uncertain or unknown attempt), the
gate in bootstrap mode, a fresh observe after any ask (the approved thing must still be the
executed thing), the write-ahead record for R2/R3 before dispatch, the executor (one local retry
when nothing was delivered because the element was not found; `scroll_into_view` once when it
was offscreen; a gate code from the executor is final), then T1 against the before of record (the
persisted one on a retry, so a late first attempt surfaces as a duplicate), the label of record,
the ledger outcome and a redacted result. Resume: a record with no outcome whose effect is present
relative to its persisted before is done already; otherwise it is a confirm with the previous
phase shown. The step's code is the verifier's; the executor's delivery is evidence.

`run_task`: presence conditions already true at task start are ticked and their steps skipped
(diff conditions only tick through the write-ahead snapshot), then the steps run under a max step
count and a screen guard (three fruitless steps on an unchanged screen stop the task; there is no
planner yet to replan).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, Literal, Protocol

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.executor import Action, physical_click_target
from steerai.action.launch import (
    APPS,
    expected_title,
    open_condition_holds,
    open_title_judges,
    title_matches,
)
from steerai.action.launcher import OpenAction
from steerai.action.live_reads import ListboxReads, twin_value_of
from steerai.action.options import option_key
from steerai.action.physical import TEXT_ROLES, PhysicalAction
from steerai.action.sanitize import field_text, normalise_newlines
from steerai.errors import ErrorCode, SteerError
from steerai.goal_match import GoalMatcher
from steerai.orchestrator.contracts import (
    CHOOSE_ROLE,
    OPTION_PARAM,
    RISKS,
    START_WINDOW,
    WIDGET_VERBS,
    GateDecision,
    GateToken,
    Plan,
    Risk,
    Step,
    StepResult,
    Subgoal,
    Target,
    executor_verb,
    subgoal_verbs,
)
from steerai.orchestrator.gate import (
    Gate,
    GateResult,
    ask_reasons,
    deny_tier,
    effective_risk,
    group_name,
    open_tier,
    params_for,
    rename_counts,
    url_query_eligible,
)
from steerai.orchestrator.identity import (
    Binding,
    bind,
    other_role_matches,
    pick,
    rebind,
    too_many,
)
from steerai.orchestrator.ledger import LedgerError, WriteAheadRecord
from steerai.orchestrator.plan_io import names_content_landmark
from steerai.orchestrator.trace import NullTrace, Trace
from steerai.verify.predicates import (
    FIELD_ROLES,
    All,
    AnyOf,
    Appeared,
    CheckState,
    CountDelta,
    Disappeared,
    FieldValue,
    Predicate,
    Present,
    TitleMatches,
    WindowExists,
    WindowGone,
    check_states,
    field_values,
    presents,
)
from steerai.verify.strength import STATE_WANTED
from steerai.verify.t1 import (
    NO_LABEL_REASONS,
    LiveReads,
    Verdict,
    WindowLike,
    evaluate,
    label_of_record,
    reads_wanted,
    same_title,
    title_holds,
    verify,
)
from steerai.verify.url_query import UrlBefore, page_loaded
from steerai.world.elements import Element
from steerai.world.fingerprint import similarity
from steerai.world.model import Snapshot, normalise_name
from steerai.world.people import MAX_CANDIDATES, lookalike_only, name_part
from steerai.world.secure import SecureMemory
from steerai.world.twins import PinKey, on_screen, pin_key, twin_set

REUSE_FRESHNESS_S = 5.0
QUIET_AFTER_TYPE_S = 1.0
"""How long the page must stay unchanged before an R2/R3 step that follows a `type` (5.11
[v2.44]): measured 2026-11-08, Wikipedia's suggestions re-render the search widget 0.39-0.89 s
after the text lands (n=5); other pages unmeasured."""
SETTLE_AFTER_TYPE_CAP_S = 4.0
"""The longest that wait lasts: a page still changing then is judged as it is (the ask and the
re-observe after it still guard the step)."""
SETTLE_POLL_S = 0.1
"""How old a decided after snapshot may be to serve as the next R0/R1 before. Unmeasured; well
inside the executors' 10 s admission cap."""
SCREEN_GUARD_THRESHOLD = 0.9
IDLE_S = 2.0
"""A step starts only after the person has been idle this long (5.1 [v1.4]; unmeasured)."""
MAX_WAIT_S = 60.0
"""Continuous input beyond this pauses the task as a takeover (unmeasured)."""
WAIT_POLL_S = 0.1
SETTLE_S = 0.5
"""A target missing right after the page changed is a timing gap first: settle this long and
re-observe before calling it absent (first real-site run 2026-11-04; unmeasured)."""
SETTLE_RETRIES = 2
_RISK_RANK = {risk: rank for rank, risk in enumerate(RISKS)}
ALIAS_ROLES: frozenset[str] = FIELD_ROLES - {"ComboBox"}
"""Text roles one app shows for another's field: a `focus` or `type` whose exact role is not
on screen resolves by its exact name among these (chooser-wiring gate; live run osg-4 named
`Edit` where Notepad shows `Document`). Never a ComboBox, either way: its value is a choose's
(W4-10), and the condition side keeps the same rule (`t1.ALIAS_FIELD_ROLES`)."""
_ALIAS_VERBS: frozenset[str] = frozenset({"focus", "type"})
MAX_SCROLLS = 2
"""Pre-gate scrolls per step: one, and one more when a lazy load pushed the target back
(measured on a real-Chrome lazy feed, #184 re-review); an endless feed then fails closed."""
OPEN_POLL_S = 0.1
NAVIGATION_VERIFY_S = 10.0
"""Verify deadline for a replacing `appeared` (a page replacing itself; 5.10 [v1.9]): a heavy
page loads slower than the 3 s step deadline (Wikipedia article: over 3.5 s, under 5 s, n=1).
Unmeasured beyond that."""
STOP_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.APP_CRASHED,
        ErrorCode.WINDOW_OTHER_DESKTOP,
        ErrorCode.SECURE_DESKTOP,
        ErrorCode.INTERRUPT_MODAL,
        ErrorCode.PERCEPTION_DEGRADED,
    }
)
"""Codes that end the task even after a dispatch: the window or the desktop is not there to
act on any more (RN-14); the ledger record stays open, so a resume confirms."""
SCREEN_GUARD_STRIKES = 3
NO_EFFECT = "no_effect"
"""An Enter the loop proved changed nothing (packet no-effect-submit-2026-11-10): the step's
evidence reason and detail, its ledger outcome, and the task's detail; the replan's trigger."""
_MISSES = frozenset({"no_match", "not_new", "not_replaced"})
"""The verify's deadline misses after which the no-effect facts are read (NE-05)."""
_DELIVERED = frozenset({Delivery.DELIVERED_UNVERIFIED, Delivery.DELIVERED_VERIFIED})
"""An Enter that reached the app: never uncertain, never a refusal (NE-05)."""
FORM_ROLES = frozenset({"Group", "Document"})
"""The roles a form container shows as: a Button submit and its composer must have the same
nearest ancestor of one of these roles (NE-10). Measured (Chrome 156, 2026-11-10): Chrome exposes
no element for an unnamed `<form>`, so on a local fixture the field and the form's own button
hang directly off the Document; on npm (n=2) the "Search packages" field and the "Search" button
share the same nearest Group (unnamed, two up); a header button sits under its own Group."""
_RISK_ORDER = {risk: index for index, risk in enumerate(RISKS)}
DEFAULT_MAX_STEPS = 20
_DIFF_PREDICATES = (Appeared, Disappeared, CountDelta, WindowGone)


class Monitor(Protocol):
    """The takeover monitor as the loop sees it (5.1 [v1.4])."""

    def last_user_input(self) -> float: ...
    def kill_requested(self) -> bool: ...
    def healthy(self) -> bool: ...
    def detail(self) -> str: ...
    def maintain(self) -> None: ...
    def counts(self) -> dict[str, int]: ...
    def mark_ask_open(self) -> None: ...
    def mark_ask_closed(self) -> None: ...


class Observer(Protocol):
    def observe(self, hwnd: int, *, stable_window: int = ..., cap_s: float = ...) -> Snapshot: ...


class ExecutorLike(Protocol):
    def execute(self, action: Any, snapshot: Snapshot) -> ActionResult: ...


class TaskLedger(Protocol):
    window_ref: str | None

    def key_for(self, step: Step) -> str: ...
    def admit(self, key: str, decision: GateDecision | None = None) -> str: ...
    def outcome_of(self, key: str) -> str | None: ...
    def closed_by(self, key: str) -> str | None: ...
    def write_ahead(
        self,
        step: Step,
        before: Snapshot,
        decision: GateDecision | None,
        chosen: dict[str, Any] | None = None,
        url_before: UrlBefore | None = None,
    ) -> WriteAheadRecord: ...
    def mark(self, step: Step, phase: Any) -> None: ...
    def record_outcome(self, step: Step, outcome: str) -> None: ...
    def read_ahead(self, step: Step) -> WriteAheadRecord | None: ...
    def before_of_record(self, step: Step, fresh: Snapshot) -> Snapshot: ...
    def set_window_ref(self, ref: str | None) -> None: ...
    def record_open(self, step: Step, window: Mapping[str, Any]) -> None: ...
    def opened_window(self, step: Step) -> dict[str, Any] | None: ...
    def opened_by_ref(self, ref: str) -> dict[str, Any] | None: ...
    def opened_records(self, ref: str) -> list[dict[str, Any]]: ...
    def start_window(self) -> dict[str, Any] | None: ...
    def earlier_verbs(self) -> dict[str, set[str]] | None: ...
    def record_open_attempt(self, step: Step, outcome: str) -> None: ...


class LauncherLike(Protocol):
    # Any: the launcher takes the probes' WindowInfo, the loop's lister yields WindowLike;
    # a Protocol parameter cannot be narrower than its implementations
    def execute(self, action: OpenAction, windows_before: Sequence[Any]) -> ActionResult: ...


@dataclass(frozen=True, slots=True)
class TaskResult:
    status: Literal["done", "stopped", "paused", "blocked"]
    results: list[StepResult]
    ticked: set[str]
    code: ErrorCode | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


def wait_for_idle(
    monitor: Monitor, *, monotonic: Callable[[], float], sleep: Callable[[float], None]
) -> str | None:
    """The one rule for anything that acts on the person's desktop (5.1 [v1.4]): the kill
    switch first (nothing is injected after the key), then the monitor's health, then the
    person's idle time, polled up to `MAX_WAIT_S`. Returns None to go ahead, or the token
    `killed`, `takeover` or the monitor's own detail."""
    started = monotonic()
    while True:
        if monitor.kill_requested():
            return "killed"
        monitor.maintain()
        if not monitor.healthy():
            return monitor.detail()
        now = monotonic()
        if now - monitor.last_user_input() >= IDLE_S:
            return None
        if now - started >= MAX_WAIT_S:
            return "takeover"
        sleep(WAIT_POLL_S)


class _StepFailureError(Exception):
    def __init__(self, code: ErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class _KilledError(_StepFailureError):
    """The person pressed the kill switch: the step is blocked by them (TK-09/23/24)."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.GATE_BLOCKED, "killed")


class _TakeoverError(_StepFailureError):
    """The person kept using the machine past the wait: the task pauses (TK-30/48)."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.GATE_PENDING_USER, "takeover")


@dataclass
class _Stage:
    """Latency accounting by stage (OR-39): policy time apart from the user's wait."""

    monotonic: Callable[[], float]
    totals: dict[str, float] = field(default_factory=dict)

    def measure(self, name: str, work: Callable[[], Any]) -> Any:
        started = self.monotonic()
        try:
            return work()
        finally:
            self.totals[name] = self.totals.get(name, 0.0) + (self.monotonic() - started) * 1000


@dataclass(frozen=True, slots=True)
class _NoEffectFacts:
    """What a `no_effect` verdict was read against, kept for the re-read before the replan's
    planner call (NE-08): the step, its approved text, the before URL of record, the before
    snapshot of record and the step's window list."""

    step: Step
    text: str
    url_before: UrlBefore
    before: Snapshot
    windows: list[WindowLike] | None


class Session:
    def __init__(  # noqa: PLR0913, PLR0915 - every collaborator injected; per-step state declared
        self,
        *,
        task_id: str,
        hwnd: int,
        observer: Observer,
        semantic: ExecutorLike,
        physical: ExecutorLike,
        gate: Gate,
        ledger: TaskLedger,
        live: LiveReads | None = None,
        windows: Callable[[], Sequence[WindowLike]] | None = None,
        trace: Trace | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        verify_deadline_s: float = 3.0,
        max_steps: int = DEFAULT_MAX_STEPS,
        monitor: Monitor | None = None,
        launcher: LauncherLike | None = None,
        window_title: Callable[[int], str] | None = None,
        # Any: a JSON-shaped identity record (hwnd, pid, start, image, title)
        identify: Callable[[int], dict[str, Any] | None] | None = None,
        foreground: Callable[[], int] | None = None,
        locked: Callable[[], bool | None] | None = None,
        bring_forward: Callable[[int], str] | None = None,
        tap_guard: Callable[[], str | None] | None = None,
        goal_matcher: GoalMatcher | None = None,
        max_risk: Risk = "R3",
        address_bar: Callable[[int], str | None] | None = None,
        document_url: Callable[[int], str | None] | None = None,
    ) -> None:
        self.max_risk: Risk = max_risk
        """The person's `--max-risk` for the whole run: a tier the screen raises past it stops
        the step before the ask (#88); R3 means no limit."""
        self._goal_matcher = goal_matcher
        """Judges each plan's steps against the goal (5.13 step 2 [v2.27]); None is `off`."""
        self._verdicts: Mapping[str, str] | None = None
        """The current plan's verdict per step id; None while goal-match is off."""
        self._done_steps: list[Step] = []
        self._preset: tuple[Plan, Mapping[str, str]] | None = None
        """Verdicts a trusted plan-cache entry carries, for exactly that plan (5.12 [v2.33])."""
        self._judge_failure: str | None = None
        """Steps this session ran, the context a continuation's judgement reads (GM-08)."""
        self._tap_guard = tap_guard
        """A reason not to tap now (held keys, accessibility filters, an elevated window
        in front, a secure desktop), or None (hunter SF-01/05/06/20)."""
        self._front_pending = False
        """A resume re-bound another window at plan start: its first step that needs the
        window in front taps it (SF-31)."""
        self._bring_forward = bring_forward
        """Brings a window the task switched to in front (the procedure name); raises
        OSError when refused (issue #38)."""
        self._locked = locked
        """The session's lock state; None means not read (pure tests only)."""
        self._step_refs: dict[str, str | None] = {}
        """Each step's window from plan order (None: the start window; SW-01)."""
        self._switched = False
        """The task moved to another window (a selection or an `open`): its R2/R3 steps need
        that window in front (SW-15); the start window's own steps keep the send path."""
        self._identity_now: Mapping[str, Any] | None = None
        """The recorded identity of the current window once the task checked it (a selection,
        an `open`, a continuation's start), re-read before a dispatch."""
        self.task_id = task_id
        self._launcher = launcher
        self._window_title = window_title
        self._identify = identify
        self._pins: dict[PinKey, tuple[int, ...]] = {}
        """Which twin field each verified `type` wrote, per window, role and name (#150,
        D-150 v2 item 4): conditions read it; a step resolve may only refuse by it."""
        self._twin_pick: str | None = None
        """How the step's target was first chosen among twins (`onscreen`, `focused`); an
        `onscreen` pick never scrolls (principal blocking 1)."""
        self._twin_rid: tuple[int, ...] | None = None
        self._address_bar = address_bar
        """Chrome's omnibox value for a window, None when missing, focused or empty (#148)."""
        self._document_url = document_url
        """The URL of the window's one top-level Document, the load signal's second rule."""
        self._url_step = False
        """The step in flight is a Chrome search submit the address bar may prove (#148)."""
        self._url_before: UrlBefore | None = None
        self._foreground = foreground
        self._goal = ""
        self._content_landmarks: tuple[str, ...] = ()
        self._monitor = monitor
        self._counts_at_step: dict[str, int] = {}
        self._secure_step = False
        self.hwnd = hwnd
        self.max_steps = max_steps
        self._steps_run = 0
        """Steps run by this session over all its plans: a replanned task is one budget."""
        self._verbs: dict[str, set[str]] = {}
        """The verbs each subgoal has held over every plan of the task (gate ruling R1): this
        session's plans, and on a resumed continuation the ledger chain's earlier links (#51)."""
        self._verbs_complete = False
        """Whether `_verbs` covers every plan of the task; a resumed continuation whose ledger
        cannot say is unknown, and no open ticks then (issue #51, gate Q2)."""
        self._observer = observer
        self._secure_memory = SecureMemory()
        """A field once seen secure stays secure for the task (WV-09)."""
        self._earlier_unmet = False
        """Whether an earlier subgoal whose steps ran is unticked, set by the plan loop for the
        step it runs: a step the element raises to R2/R3 then never starts (#190 review)."""
        self._ticks: tuple[Plan, set[str]] | None = None
        """The plan and its ticked subgoals, set by the plan loop for the step it runs, so an
        R2/R3 step reads the ticked values and states again first (W4-04)."""
        self._reset: set[str] = set()
        """The ticked subgoals the step's re-read found contradicted (W4-04)."""
        self._semantic = semantic
        self._physical = physical
        self._gate = gate
        self._ledger = ledger
        self._live = live
        self._windows = windows
        self._trace: Trace = NullTrace() if trace is None else trace
        self._monotonic = monotonic
        self._sleep = sleep
        self._verify_deadline_s = verify_deadline_s
        self._last_after: tuple[Snapshot, float] | None = None
        self._last_pair: tuple[Snapshot, Snapshot] | None = None
        self._typed_at: float | None = None
        """When the last `type` was dispatched: the next R2/R3 step waits for a quiet page."""
        self._proof: tuple[Predicate, Verdict] | None = None
        """The step's own verified postcondition, as bound by the gate (#124)."""
        self._url_proof: tuple[Predicate, Verdict, str] | None = None
        """The step's expect, counted by the address bar (#148): it ticks only the subgoal whose
        condition is exactly that expect (5.10 [v2.48])."""
        self._windows_at_step: list[WindowLike] | None = None
        self._no_effect_facts: _NoEffectFacts | None = None
        """The last step's `no_effect` facts, until the next step begins."""
        self._scrolled: str | None = None
        self._act_evidence: dict[str, Any] = {}
        self._sent_in_act = False
        self._binding: Binding | None = None
        self._candidates: tuple[str, list[Element]] | None = None
        self._aliased: str | None = None
        """The text role the step resolved by alias, kept for its later re-resolves."""

    def _observe(self, **kwargs: Any) -> Snapshot:
        """Every observe of the task's window: a field once seen secure stays secure by its
        RuntimeId (WV-09), so "Show password" never makes its value readable."""
        return self._secure_memory.apply(self._observer.observe(self.hwnd, **kwargs))

    # ---- one step ----------------------------------------------------------------------------

    def run_step(self, step: Step) -> StepResult:
        if step.verb == "open":
            return self._run_open(step)
        stage = _Stage(self._monotonic)
        risk = effective_risk(step)
        evidence: dict[str, Any] = {"risk": risk}
        gate_stage: GateToken = "unreached"
        dispatched: ActionResult | None = None
        windows_before = list(self._windows()) if self._windows else None
        self._begin_step(windows_before)
        recorded = False
        try:
            self._enter_window(step, evidence)
            key = self._ledger.key_for(step)
            self._act_evidence = evidence
            before, risk = self._before_at_ceiling(step, key, risk, stage, evidence)
            _measured_provider(step, before)
            previous = self._admission(step, key, risk, before)
            if previous == "done":
                if step.verb in WIDGET_VERBS:
                    self._state_holds(step, before, stage, evidence)
                return self._done_already(step, key, stage, evidence)
            before, element_id = self._resolved_target(step, before, stage, evidence)
            self._secure_step = before.element(element_id).secure
            risk, previous = self._readmitted(
                step,
                key=key,
                risk=risk,
                before=before,
                element_id=element_id,
                evidence=evidence,
                previous=previous,
            )
            self._in_front(risk)
            gate_result = stage.measure(
                "gate", lambda: self._decide(step, key, before, previous, element_id, risk=risk)
            )
            gate_stage = gate_result.outcome
            _note_reasons(evidence, gate_result)
            if gate_result.outcome in ("block", "pending"):
                return self._gate_stopped(step, gate_result, stage, evidence)
            decision, expect = self._admitted(key, previous, gate_result)
            waited = self._boundary(evidence)
            if gate_result.outcome == "confirm" or waited:
                # the approved thing must be the executed thing (OR-31); a wait that ended after
                # the observe restarts the step from observe (TK-03)
                token = (
                    "screen_changed_during_ask"
                    if gate_result.outcome == "confirm"
                    else "screen_changed_during_wait"
                )
                asked = element_id
                before, element_id = stage.measure(
                    "observe",
                    lambda: self._reobserve(step, before, evidence, asked=asked, mismatch=token),
                )
                before, element_id = self._still_shown(
                    step, before, element_id, stage, evidence, asked=gate_result.outcome, risk=risk
                )
                if self._binding is not None:
                    # the bound row's text may have moved on: the postcondition follows it,
                    # through the gate's own strength rung (never a weaker check)
                    expect = self._rebuilt(step, risk, before, element_id)
                if waited:
                    evidence["restarted"] = True
            before_of_record, recorded = self._record(step, risk, before, decision, expect)
            action, before, retries = stage.measure(
                "act", lambda: self._act(step, risk, before, element_id)
            )
            dispatched = action
            evidence["retries"] = retries
            evidence["delivery"] = action.delivery.value
            if self._scrolled is not None:
                evidence["scrolled"] = self._scrolled
            undelivered = self._undelivered(
                step, stage, evidence, gate=gate_result.outcome, action=action, risk=risk
            )
            if undelivered is not None:
                return undelivered
            return self._verified(
                step,
                stage,
                evidence,
                key=key,
                expect=expect,
                before_of_record=before_of_record,
                windows_before=windows_before,
                action=action,
                risk=risk,
                gate=gate_result.outcome,
            )
        except (_StepFailureError, SteerError) as failure:
            # a refusal before anything was sent, or the world model failing to observe (RN-14):
            # a result either way, with the dispatched action when there is one
            release = recorded and dispatched is None and not self._sent_in_act
            return (
                self._refused(
                    step,
                    stage,
                    evidence,
                    gate=gate_stage,
                    failure=failure,
                    risk=risk,
                    release=release,
                )
                if isinstance(failure, _StepFailureError)
                else self._failed(
                    step, stage, evidence, gate=gate_stage, dispatched=dispatched, failure=failure
                )
            )

    # ---- open (packet open-multiwindow-2026-11-06) -----------------------------------------------

    def _run_open(self, step: Step) -> StepResult:
        """No element to resolve: the gate on the code-owned title, the window list right
        before the launch (P4), the launcher's own window adopted only once its live title
        matches (P5, OW-06/09), the window recorded at any tier (P7), a failure stops (P6)."""
        stage = _Stage(self._monotonic)
        tier = open_tier(self._goal, step)
        if tier != step.risk:
            step = replace(step, risk=tier)  # the gate and the ledger see the content's tier
        risk = effective_risk(step)
        if step.id in self._step_refs:
            self._ledger.set_window_ref(self._step_refs[step.id])
        key = self._ledger.key_for(step)
        evidence: dict[str, Any] = {"risk": risk}
        gate_stage: GateToken = "unreached"
        self._begin_step(None)
        recorded = False
        try:
            self._boundary(evidence)
            self._act_evidence = evidence
            if self._ledger.admit(key) == "done":
                self._rebind_opened(step)
                evidence["window"] = "rebound"
                return self._done_already(step, key, stage, evidence)
            prior = self._ledger.outcome_of(key)
            if prior is not None and prior != "not_delivered" and risk in ("R0", "R1"):
                # an earlier attempt launched and may have opened a window: never a silent second
                # launch; the person confirms with the earlier attempt shown (tester gap)
                step = replace(step, risk="R2")
                risk = "R2"
                evidence["risk"] = risk
                key = self._ledger.key_for(step)
            before = stage.measure("observe", lambda: self._before(risk, evidence))
            previous = self._admission(step, key, risk, before)
            self._within_limit(risk)
            gate_result = stage.measure(
                "gate", lambda: self._decide(step, key, before, previous, None, risk=risk)
            )
            gate_stage = gate_result.outcome
            _note_reasons(evidence, gate_result)
            if gate_result.outcome in ("block", "pending"):
                return self._gate_stopped(step, gate_result, stage, evidence)
            decision, _expect = self._admitted(key, previous, gate_result)
            self._boundary(evidence)
            _before_of_record, recorded = self._record(step, risk, before, decision)
            return self._launch(step, risk, stage, evidence, gate=gate_result.outcome)
        except (_StepFailureError, SteerError) as failure:
            if isinstance(failure, _StepFailureError):
                return self._refused(
                    step,
                    stage,
                    evidence,
                    gate=gate_stage,
                    failure=failure,
                    risk=risk,
                    release=recorded and not self._sent_in_act,
                )
            return self._failed(
                step, stage, evidence, gate=gate_stage, dispatched=None, failure=failure
            )

    def _launch(
        self,
        step: Step,
        risk: Risk,
        stage: _Stage,
        evidence: dict[str, Any],
        *,
        gate: GateToken,
    ) -> StepResult:
        if self._launcher is None or self._window_title is None or self._identify is None:
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "no_launcher")
        if step.target is None:  # enforced by Step.__post_init__; kept for the type checker
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "no_target")
        site, text = step.params.get("site"), step.params.get("text")
        title = expected_title(step.target.name, site, text)
        if self._windows is None:
            # with no list, the person's own windows would count as new (review note)
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "no_window_list")
        if risk in ("R2", "R3"):
            try:
                self._ledger.mark(step, "acting")
            except LedgerError as exc:
                raise _StepFailureError(ErrorCode.GATE_BLOCKED, _wal_detail(exc)) from exc
        windows_before = list(self._windows())  # the last thing before the launch (P4)
        launcher = self._launcher
        launched_at = self._monotonic()
        action = stage.measure(
            "act",
            lambda: launcher.execute(
                OpenAction(app=step.target.name if step.target else "", site=site, text=text),
                windows_before,
            ),
        )
        self._sent_in_act = bool(action.steps)
        hwnd = action.evidence.get("hwnd")
        if not isinstance(hwnd, int):
            self._close_open(step, risk, action.delivery.value, launched=bool(action.steps))
            evidence["detail"] = str(action.evidence.get("detail", "no_new_window"))
            if evidence["detail"] == "several_new_windows" and self._monitor is not None:
                # did the person act after the launch? (#75: a window they opened themselves)
                last = self._monitor.last_user_input()
                evidence["input_since_launch"] = last >= launched_at
            return self._finish(step, stage, evidence, gate=gate, action=action, code=action.code)
        holds = stage.measure("verify", lambda: self._title_arrives(hwnd, title))
        identity = self._identify(hwnd) if holds else None
        images = APPS[step.target.name].process_names if step.target.name in APPS else frozenset()
        if identity is not None and str(identity.get("image", "")).lower() not in images:
            identity = None  # the window is not the app's after all
        if identity is not None and identity.get("cloaked") is not False:
            identity = None  # hidden by DWM, or DWM did not answer: not verified (CU-09)
        if identity is None:
            self._close_open(step, risk, Delivery.DELIVERED_UNVERIFIED.value, launched=True)
            evidence["detail"] = "open_unverified"
            return self._finish(
                step,
                stage,
                evidence,
                gate=gate,
                action=action,
                verify_code=ErrorCode.VERIFY_TIMEOUT,
                code=ErrorCode.VERIFY_TIMEOUT,
            )
        self._close(step, risk, Delivery.DELIVERED_VERIFIED.value)
        self._ledger.record_open(step, identity)  # the observed title, digested
        self._adopt(hwnd, f"opened:{step.id}")
        self._identity_now = identity
        self._switched = True
        evidence["window"] = "opened"
        if self._foreground is not None:
            evidence["foreground_after_open"] = self._foreground() == hwnd
        return self._finish(
            step, stage, evidence, gate=gate, action=action, label_kind="t1", ok=True
        )

    def _close_open(self, step: Step, risk: Risk, outcome: str, *, launched: bool) -> None:
        """An R2/R3 attempt closes its write-ahead record; a launched R0/R1 attempt is still
        recorded, so a resume knows a window may already be open (tester gap)."""
        if risk in ("R2", "R3"):
            self._close(step, risk, outcome)
        elif launched:
            self._ledger.record_open_attempt(step, outcome)

    def _title_arrives(self, hwnd: int, title: str) -> bool:
        """The launcher's own window shows the code-owned title (counter stripped, full-string),
        re-read every poll until the navigation deadline; the kill ends the wait."""
        read = self._window_title
        if read is None:
            return False
        deadline = self._monotonic() + max(self._verify_deadline_s, NAVIGATION_VERIFY_S)
        while True:
            if self._monitor is not None and self._monitor.kill_requested():
                raise _KilledError
            if title_matches(read(hwnd), title):
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleep(OPEN_POLL_S)

    def _adopt(self, hwnd: int, ref: str | None) -> None:
        """The opened window becomes the current one; nothing observed before carries over."""
        self.hwnd = hwnd
        self._last_after = None
        self._last_pair = None
        self._pins.clear()
        self._ledger.set_window_ref(ref)

    def _rebind_opened(self, step: Step) -> None:
        """A resume: the recorded window again, by identity (handle, pid, process start time,
        image), never by title (P7, decision 3); anything else stops `window_gone`."""
        self._rebind(self._ledger.opened_window(step), f"opened:{step.id}")

    def _rebind_ref(self, *, continuation: bool) -> None:
        """A continuation plan, or its resume, starts in the window its link names, rebound by
        identity before anything is observed (replan item 3); a link in the start window is
        checked the same way once its identity was recorded (SW-26)."""
        ref = self._ledger.window_ref
        if ref is not None:
            self._front_pending = self._rebind(self._record_of(ref), ref)
            self._switched = True  # a window the task opened, as on the first run (SW-15)
        elif continuation and self._ledger.start_window() is not None:
            self._rebind(self._record_of(None), None)

    def _rebind(self, record: Mapping[str, Any] | None, ref: str | None) -> bool:
        """Re-bind by identity; True when the current window changed."""
        hwnd = self._checked(record)
        changed = hwnd != self.hwnd
        if changed:
            self._adopt(hwnd, ref)
        else:
            self._ledger.set_window_ref(ref)  # the same window: what was observed holds
        self._identity_now = record
        return changed

    def _enter_window(self, step: Step, evidence: dict[str, Any]) -> None:
        """The window first (it is part of the key), then the boundary, then the tap: the
        person's typing never moves into the window brought forward (SW-07, issue #38)."""
        self._refuse_locked()  # a locked session stops as such, never as window_gone
        changed = self._select(step, evidence) or self._front_pending
        self._boundary(evidence)
        if not changed:
            return
        # a step that does not need the window in front carries the switch to the first one
        # that does (tester T-01); a skipped or refused tap is tried again there (N6)
        self._front_pending = True
        if _needs_front(step):
            self._to_front(evidence)
            self._front_pending = evidence.get("foreground_after_select") is False

    def _to_front(self, evidence: dict[str, Any]) -> None:
        """Bring the window just switched to in front, then re-read the lock (SW-07). A
        refused tap is evidence, never a stop: the executor's own checks decide (gate Q2)."""
        if self._bring_forward is None:
            return
        if self._monitor is not None and self._monitor.kill_requested():
            raise _KilledError  # nothing injected after the key (SF-32)
        if self._identity_now is not None:
            self._checked(self._identity_now)  # changed while the boundary waited (SF-09)
        try:
            try:
                reason = None if self._tap_guard is None else self._tap_guard()
            except OSError:
                reason = "guard_unreadable"  # unclear: no tap, the step goes on (gate Q2)
            if reason == "secure_desktop":
                raise _StepFailureError(ErrorCode.SECURE_DESKTOP, "secure_desktop")
            if reason is not None:
                evidence["foreground_after_select"] = False
                evidence["tap_skipped"] = reason
                return
            try:
                procedure = self._bring_forward(self.hwnd)
            except OSError:
                evidence["foreground_after_select"] = False
            else:
                # an unread foreground is never a success (gate blocking item 1)
                front = self._foreground is not None and self._foreground() == self.hwnd
                evidence["foreground_after_select"] = procedure if front else False
        finally:
            self._refuse_locked()  # a lock that came with the tap stops before observe

    def _checked(self, record: Mapping[str, Any] | None) -> int:
        """The recorded window, still the same one: handle, pid, process start and image
        equal, a visible top-level window, readable, not minimized (SW-12/13/14)."""
        hwnd = None if record is None else record.get("hwnd")
        current = (
            self._identify(hwnd) if self._identify is not None and isinstance(hwnd, int) else None
        )
        if record is None or current is None or not isinstance(hwnd, int):
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "window_gone")
        readable = all(r.get("start") is not None and r.get("image") for r in (record, current))
        if not readable:
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "identity_unreadable")
        same = all(current.get(k) == record.get(k) for k in ("pid", "start", "image"))
        if not same or current.get("visible") is not True or current.get("root") is not True:
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "window_gone")
        cloaked = current.get("cloaked")
        if cloaked is True:
            # DWMWA_CLOAKED: on another virtual desktop (SF-10)
            raise _StepFailureError(ErrorCode.WINDOW_OTHER_DESKTOP, "window_other_desktop")
        if cloaked is not False:
            # DWM did not answer: unclear, never visible (issue #48)
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "identity_unreadable")
        if current.get("minimized") is not False:
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "minimized")
        return hwnd

    def _select(self, step: Step, evidence: dict[str, Any]) -> bool:
        """The step's window from plan order, re-bound by identity; naming the current
        window is accepted and still checked (gate Q2). True when the window changed."""
        if step.id in self._step_refs:
            ref = self._step_refs[step.id]
        elif step.window is not None:
            ref = _ledger_ref(step.window)
        else:
            return False
        if step.window is None and ref == self._ledger.window_ref:
            return False
        record = self._record_of(ref)
        if (
            ref is None
            and record is not None
            and str(record.get("image", "")).lower() == FRAME_HOST
        ):
            # every UWP frame shares the host's pid and image: `window: start` waits for a
            # measurement (SW-11); a continuation there still runs (review item 1)
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "frame_host")
        changed = self._rebind(record, ref)
        self._switched = True
        evidence["window"] = "selected"
        return changed

    def _record_of(self, ref: str | None) -> Mapping[str, Any] | None:
        if ref is None:
            return self._ledger.start_window()
        records = self._ledger.opened_records(ref)
        if len(records) > 1:
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "window_ambiguous")
        return records[0] if records else None

    def window_label(self) -> str:
        """The current window for an ask: the registry app by its image (else the image),
        the reference, then the live title capped by code (SW-06); empty when nothing is
        known."""
        current = self._identify(self.hwnd) if self._identify is not None else None
        if current is None:
            return ""
        image = str(current.get("image", "")).lower()
        app = next((key for key, spec in APPS.items() if image in spec.process_names), image)
        ref = self._ledger.window_ref or START_WINDOW
        title = " ".join(str(current.get("title", "")).split())[:WINDOW_TITLE_CHARS]
        return f"{app} ({ref}): {title or '(untitled)'}"

    def live_title(self, ref: str) -> str | None:
        """The live title of a task window named by reference, None unless its recorded
        identity still holds (SW-36): the planner never sees a window that is not the
        task's any more."""
        try:
            hwnd = self._checked(self._record_of(_ledger_ref(ref)))
        except _StepFailureError:
            return None
        current = self._identify(hwnd) if self._identify is not None else None
        title = None if current is None else current.get("title")
        return title if isinstance(title, str) else None

    def _within_limit(self, risk: Risk) -> None:
        """The run's `--max-risk`, at the tier the step really runs at (#88)."""
        if RISKS.index(risk) > RISKS.index(self.max_risk):
            raise _StepFailureError(ErrorCode.GATE_BLOCKED, "over_max_risk")

    def _in_front(self, risk: Risk) -> None:
        """An R2/R3 step in a window the task selected or opened is in front when its ask
        opens: the person sees where a send lands (SW-15). The switch tap (`_to_front`, [v2.5])
        runs before this check; it stays the check after the tap. The start window's own steps
        keep the measured send path (PR #33 review item 3)."""
        self._within_limit(risk)
        front = self._foreground
        selected = self._switched
        if risk in ("R2", "R3") and selected and front is not None and front() != self.hwnd:
            raise _StepFailureError(ErrorCode.FOCUS_LOST, "not_foreground")

    def _still_the_window(self) -> None:
        """The last check before a dispatch: the window is the one selected or opened,
        after any ask or wait (SW-16)."""
        if self._identity_now is not None:
            self._checked(self._identity_now)

    def _holds_in_window(self, predicate: Predicate) -> bool:
        """An `open`'s subgoal in the window it opened: every element there is new, so a
        presence check is the effect; titles are read live."""
        if isinstance(predicate, All | AnyOf):
            parts = (self._holds_in_window(part) for part in predicate.parts)
            return all(parts) if isinstance(predicate, All) else any(parts)
        if isinstance(predicate, WindowExists | TitleMatches):
            title = "" if self._window_title is None else self._window_title(self.hwnd)
            if isinstance(predicate, WindowExists):
                return bool(title) and title_matches(title, predicate.name_pattern)
            return bool(title) and title_holds(predicate.pattern, title)
        try:
            snapshot = self._observe()
        except SteerError:
            return False
        if not isinstance(predicate, _DIFF_PREDICATES):
            return self._holds_now(predicate, snapshot)
        # the verifier, against an empty before of the new window: scope, count, case
        # and replace rules all still apply (review item 3)
        empty = replace(snapshot, id=f"{snapshot.id}-empty", elements=[], node_count=0, handles=[])
        return self._subgoal_holds(predicate, empty, snapshot)

    # ---- stages ---------------------------------------------------------------------------------

    def _verified(  # noqa: PLR0913 - the step's state after the dispatch
        self,
        step: Step,
        stage: _Stage,
        evidence: dict[str, Any],
        *,
        key: str,
        expect: Predicate,
        before_of_record: Snapshot,
        windows_before: list[WindowLike] | None,
        action: ActionResult,
        risk: Risk,
        gate: GateToken,
    ) -> StepResult:
        """T1 against the before of record, the label of record, the ledger outcome."""
        verdict, after = stage.measure(
            "verify", lambda: self._verify(expect, before_of_record, windows_before)
        )
        renamed_at_risk = risk in ("R2", "R3") and verdict.holds and "renamed" in verdict.evidence
        if renamed_at_risk and self._rename_counts(step, expect, risk, before_of_record):
            # a search submit's page renamed to the framed query (5.10 [v2.39])
            renamed_at_risk = False
            verdict = replace(verdict, evidence={**verdict.evidence, "reason": "renamed_submit"})
        if renamed_at_risk:
            # a page renamed in place keeps its RuntimeId: no new element, so never the
            # effect of an R2/R3 step (5.10 [v1.2]); unverified, and no label (review item 2)
            reason = {**verdict.evidence, "reason": "renamed_at_risk"}
            verdict = replace(verdict, holds=False, code=ErrorCode.VERIFY_TIMEOUT, evidence=reason)
        url_counted = no_effect = False
        if self._url_step and not verdict.holds:
            # a Chrome search submit whose title is in no frame: the address bar's query
            # parameter is the proof, never a label (#148 design D); read once, after the
            # verify's deadline, and shared with the no-effect facts
            url_now = self._read_url()
            url_counted, url_evidence = self._url_counts(step, before_of_record, after, url_now)
            if url_counted:
                verdict = replace(verdict, holds=True, code=None, evidence=url_evidence)
            else:
                verdict = replace(verdict, evidence={**verdict.evidence, **url_evidence})
                no_effect = self._no_effect(
                    step,
                    verdict,
                    action,
                    evidence=evidence,
                    before=before_of_record,
                    after=after,
                    url_now=url_now,
                    windows_before=windows_before,
                )
            if no_effect:
                unchanged = _verify_deadline(expect, self._verify_deadline_s)
                tokens = {"reason": NO_EFFECT, "detail": NO_EFFECT, "unchanged_s": unchanged}
                verdict = replace(
                    verdict,
                    holds=False,
                    code=ErrorCode.VERIFY_FAILED,
                    evidence={**verdict.evidence, **tokens},
                )
            self._url_proof = (expect, verdict, str(step.params["text"])) if url_counted else None
        label = (
            None
            if renamed_at_risk or url_counted or no_effect
            else label_of_record(
                verdict, predicate=expect, action_key=key, before=before_of_record, after=after
            )
        )
        self._remember(before_of_record, after)
        outcome = self._outcome(verdict.holds, action, verdict.evidence.get("reason"))
        try:
            self._close(step, risk, outcome)
        except LedgerError as exc:
            # the action ran; the attempt stays open in the ledger (unknown next time)
            evidence["detail"] = _wal_detail(exc)
            return self._finish(
                step,
                stage,
                evidence,
                gate=gate,
                action=action,
                verify_code=verdict.code,
                label_kind=label.kind if label else None,
                code=ErrorCode.GATE_BLOCKED,
            )
        evidence.update({k: v for k, v in verdict.evidence.items() if k != "level"})
        evidence["level"] = verdict.level
        if no_effect:
            evidence["outcome"] = outcome  # the ledger closed it so: the replan's second token
        if step.verb in WIDGET_VERBS and before_of_record.title != after.title:
            evidence["retitled"] = True  # a token, never the titles (W3-01, W4-01)
        # already in the wanted state is nothing sent and counts as success (5.9 [v0.7])
        ok = verdict.holds and (action.delivery is not Delivery.NOT_DELIVERED or action.skipped)
        if action.evidence.get("detail") == "left_expanded":
            # the box never read Collapsed: the task stops whatever the value reads (W4-02)
            evidence["detail"] = "left_expanded"
            ok = False
        if ok and step.verb in ("type", "choose") and step.target is not None:
            # a choose pins its twin like a type, so its value condition reads it (W4-11)
            self._pin(step.target, verdict.level)
        return self._finish(
            step,
            stage,
            evidence,
            gate=gate,
            action=action,
            verify_code=verdict.code,
            label_kind=label.kind if label else None,
            ok=ok,
            code=None if ok else (verdict.code or action.code),
        )

    def _record(
        self,
        step: Step,
        risk: Risk,
        before: Snapshot,
        decision: GateDecision,
        expect: Predicate | None = None,
    ) -> tuple[Snapshot, bool]:
        """The write-ahead record before any R2/R3 dispatch; (before of record, written)."""
        self._url_step, self._url_before = False, None
        if risk not in ("R2", "R3"):
            return before, False
        try:
            before_of_record = self._ledger.before_of_record(step, before)
            chosen = self._binding.to_record() if self._binding is not None else None
            if self._url_eligible(step, expect, risk, before_of_record):
                self._url_step = True
                self._url_before = self._url_before_of(step)
            self._ledger.write_ahead(
                step, before_of_record, decision, chosen=chosen, url_before=self._url_before
            )
        except LedgerError as exc:
            raise _StepFailureError(ErrorCode.GATE_BLOCKED, _wal_detail(exc)) from exc
        return before_of_record, True

    def _undelivered(  # noqa: PLR0913 - the step's state after the dispatch
        self,
        step: Step,
        stage: _Stage,
        evidence: dict[str, Any],
        *,
        gate: GateToken,
        action: ActionResult,
        risk: Risk,
    ) -> StepResult | None:
        """A dispatch the executor refused (gate codes are final) or never issued: the result,
        with the ledger closed; None when the action went out and must be verified."""
        if action.code in (ErrorCode.GATE_BLOCKED, ErrorCode.GATE_PENDING_USER):
            # a refusal carries `detail`; a typing stop carries `reason` (killed mid-type, D2)
            token = action.evidence.get("detail", action.evidence.get("reason"))
            if token is not None:
                evidence["detail"] = token
            self._close(step, risk, action.delivery.value)
        elif action.delivery is Delivery.NOT_DELIVERED and not action.steps:
            detail = action.evidence.get("detail")
            if isinstance(detail, str):
                evidence.setdefault("detail", detail)  # a refusal names its cause (#149)
            self._close(step, risk, "not_delivered")
        elif step.verb == "choose" and _unchosen(action):
            # no Select went out, or the box was left expanded: a refusal with its detail, no
            # T1; only a not_delivered (the Collapse read back Collapsed) releases it (W4-06)
            detail = action.evidence.get("detail")
            if isinstance(detail, str):
                evidence["detail"] = detail
            self._close(step, risk, action.delivery.value)
        else:
            return None
        return self._finish(
            step,
            stage,
            evidence,
            gate=gate,
            action=action,
            verify_code=None,
            label_kind=None,
            code=action.code,
        )

    def _rebuilt(self, step: Step, risk: Risk, before: Snapshot, element_id: int) -> Predicate:
        params = params_for(step, element_id, self._composer(step, before), self._binding)
        checked = self._gate.postcondition(step, self._expect_of(step), risk, params)
        if isinstance(checked, GateResult):
            detail = str(checked.evidence.get("detail", "weak_predicate"))
            raise _StepFailureError(checked.code or ErrorCode.GATE_BLOCKED, detail)
        return checked

    def _ceiling(self, step: Step, key: str, before: Snapshot, risk: Risk) -> Risk:
        """For a person target, the tier admission runs at: the highest tier any candidate
        row could raise the step to (PT-31), and at least R2 whenever the key already has a
        record or an outcome, so an earlier raised attempt is found even when the raising
        row has left the screen (never bypassed, 5.8 [v1.6])."""
        target = step.target
        if target is None:
            return risk
        if target.match != "person":
            # the ids and the group names of every element the exact target could resolve to
            # (#84, W3-03), so a raised step never runs on a reused R0/R1 before
            ids = self._resolvable_ids(target, before)
            exact: list[Risk] = [effective_risk(step, element_ids=ids) if ids else risk]
            exact += [
                effective_risk(step, group_name=group)
                for group in _group_names(before, self._resolvable(target, before))
            ]
            return max(exact, key=_RISK_ORDER.__getitem__)
        found = pick(before, target)
        self._candidates = (before.id, found)
        tiers: list[Risk] = [risk]
        if len(found) <= MAX_CANDIDATES:
            # the raw name part: the fold makes marks like U+00A1 letters and joins words
            tiers += [effective_risk(step, name_part(c.name)) for c in found]
        if self._key_has_history(step, key):
            tiers.append("R2")
        return max(tiers, key=_RISK_ORDER.__getitem__)

    def _key_has_history(self, step: Step, key: str) -> bool:
        try:
            return (
                self._ledger.outcome_of(key) is not None
                or self._ledger.read_ahead(step) is not None
            )
        except LedgerError:
            return True  # unreadable means unknown: admission decides

    def _settled_tier(
        self, step: Step, ceiling: Risk, automation_id: str = "", group: str | None = None
    ) -> Risk:
        """The bound row's own tier, never below the plan's (PT-31); for any target, raised by
        the resolved element's own automation id (#84) and its group's name (W3-03)."""
        ids = (automation_id,) if automation_id else ()
        if self._binding is None:
            raised = effective_risk(step, element_ids=ids, group_name=group)
            return max(ceiling, raised, key=_RISK_ORDER.__getitem__)
        return effective_risk(step, self._binding.shown_name_part, ids, group_name=group)

    def _readmitted(  # noqa: PLR0913 - the step's admission state, passed once by keyword
        self,
        step: Step,
        *,
        key: str,
        risk: Risk,
        before: Snapshot,
        element_id: int,
        evidence: dict[str, Any],
        previous: str | None,
    ) -> tuple[Risk, str | None]:
        """The settled tier, recorded; when the element that resolved (after the settle)
        raises the step into R2/R3 past the tier admission ran at, admission runs again (#84).
        Returns the tier and the admission answer that stands (the first, unless re-run)."""
        element = before.element(element_id)
        group = group_name(before, element)
        settled = self._settled_tier(step, risk, element.automation_id, group)
        evidence["risk"] = settled
        if settled in ("R2", "R3") and self._earlier_unmet:
            # the [v2.36] precondition rule at the tier the element raised the step to (a
            # group name, a resolved automation id): nothing dispatched, no ask
            self._trace_tick(step.subgoal, "none", detail="precondition_unmet")
            raise _StepFailureError(ErrorCode.VERIFY_FAILED, "precondition_unmet")
        if settled in ("R2", "R3"):
            self._reread_ticks(before)
        if _RISK_ORDER[settled] > _RISK_ORDER[risk] and settled in ("R2", "R3"):
            previous = self._admission(step, key, settled, before)
            if previous == "done":
                # done under a key the plan's own tier never admitted: stop for the person,
                # nothing is dispatched (a rerun on a page that raised the element)
                raise _StepFailureError(ErrorCode.GATE_BLOCKED, "done_at_raised_tier")
        return settled, previous

    @staticmethod
    def _resolvable_ids(target: Target, snapshot: Snapshot) -> tuple[str, ...]:
        if target.element_id is not None:
            try:
                return (snapshot.element(target.element_id).automation_id,)
            except SteerError:
                return ()
        return tuple(
            e.automation_id for e in Session._resolvable(target, snapshot) if e.automation_id
        )

    @staticmethod
    def _resolvable(target: Target, snapshot: Snapshot) -> list[Element]:
        """The elements an exact target could resolve to: its fixed id, else role and name."""
        if target.element_id is not None:
            try:
                return [snapshot.element(target.element_id)]
            except SteerError:
                return []
        wanted = normalise_name(target.name).casefold()
        return [
            e
            for e in snapshot.elements
            if e.role == target.role and normalise_name(e.name).casefold() == wanted
        ]

    def _decide(  # noqa: PLR0913 - the step's state at the gate
        self,
        step: Step,
        key: str,
        before: Snapshot,
        previous: str | None,
        element_id: int | None,
        *,
        risk: Risk,
    ) -> GateResult:
        """The gate's decision; while an R2/R3 gate may be asking, the person's input is their
        answer, not a takeover (TK-07; the console brackets its own read as well)."""
        verdict = self._verdict(step)
        asking = self._monitor is not None and (
            risk in ("R2", "R3") or bool(ask_reasons(step, self._goal, verdict))
        )
        if asking and self._monitor is not None:
            self._monitor.mark_ask_open()
        try:
            return self._gate.decide(
                step,
                action_key=key,
                expect=self._expect_of(step),
                target_pid=before.pid,
                previous_attempt=previous,
                target_id=element_id,
                composer_id=self._composer(step, before),
                bound=self._binding,
                window=self.window_label(),
                goal=self._goal,
                verdict=verdict,
                element_automation_id=(
                    before.element(element_id).automation_id if element_id is not None else ""
                ),
                group_name=(
                    group_name(before, before.element(element_id))
                    if element_id is not None
                    else None
                ),
            )
        finally:
            if asking and self._monitor is not None:
                self._monitor.mark_ask_closed()

    def _boundary(self, evidence: dict[str, Any]) -> bool:
        """Before anything that acts: the kill switch, the monitor's health, and the person's
        idle time (5.1 [v1.4], TK-22/29/30/31). Returns whether it waited. A locked or
        unknown session is never acted on (issue #31)."""
        self._refuse_locked()
        monitor = self._monitor
        if monitor is None:
            return False
        started = self._monotonic()
        held = wait_for_idle(monitor, monotonic=self._monotonic, sleep=self._sleep)
        if held == "killed":
            raise _KilledError
        if held == "takeover":
            raise _TakeoverError
        if held is not None:
            raise _StepFailureError(ErrorCode.GATE_BLOCKED, held)
        waited_s = self._monotonic() - started
        if waited_s > 0:
            evidence["waited_s"] = round(evidence.get("waited_s", 0.0) + waited_s, 3)
            # Win+L is input: a lock that came while the person was busy is read now
            self._refuse_locked()
        return waited_s > 0

    def _refuse_locked(self) -> None:
        if self._locked is not None and self._locked() is not False:
            raise _StepFailureError(ErrorCode.SECURE_DESKTOP, "session_locked")

    def _reobserve(
        self,
        step: Step,
        before: Snapshot,
        evidence: dict[str, Any],
        *,
        asked: int,
        mismatch: str = "screen_changed_during_ask",
    ) -> tuple[Snapshot, int]:
        """The approved thing must be the executed thing (OR-31, #89): the same window, and for
        an exact or aliased target the same element (RuntimeId and role) as the one asked
        about; a page that moves the target's label onto another element, or removes it, is a
        changed screen, never a click elsewhere."""
        fresh = self._observe_fresh(evidence)
        if fresh.title != before.title:
            raise _StepFailureError(ErrorCode.FOCUS_LOST, mismatch)
        # no settle here (OR-31): a target gone while the ask was open is a changed screen
        element_id = self._same_target(step, fresh, before.element(asked), mismatch, evidence)
        self._secure_step = fresh.element(element_id).secure
        return fresh, element_id

    def _same_target(
        self,
        step: Step,
        snapshot: Snapshot,
        asked: Element,
        mismatch: str,
        evidence: dict[str, Any] | None = None,
    ) -> int:
        """The target resolved again in `snapshot`, which must be the element asked about
        (#89): for an exact or aliased target the same RuntimeId and role; a person target
        keeps its identity binding (`rebind`). Gone, ambiguous or another element is the
        `mismatch` token under `focus_lost`, never a dispatch elsewhere."""
        try:
            element_id = self._resolve_again(step, snapshot, evidence, asked=asked.runtime_id)
        except _StepFailureError as failure:
            if failure.detail in ("no_match", "ambiguous_target"):
                raise _StepFailureError(ErrorCode.FOCUS_LOST, mismatch) from failure
            raise
        person = step.target is not None and step.target.match == "person"
        if not person and not _same_element(asked, snapshot.element(element_id)):
            raise _StepFailureError(ErrorCode.FOCUS_LOST, mismatch)
        return element_id

    def _refused(  # noqa: PLR0913 - the step's state at the refusal
        self,
        step: Step,
        stage: _Stage,
        evidence: dict[str, Any],
        *,
        gate: GateToken,
        failure: _StepFailureError,
        risk: Risk,
        release: bool,
    ) -> StepResult:
        """A step that stopped before anything was sent: before the gate the token is
        `unreached`; after, the gate's own outcome; the person's own refusals (a kill at the
        ask, `none_chosen`) are a block; a pending ask pauses; a record with nothing sent is
        closed `not_delivered` so the key is released."""
        if release:
            try:
                self._close(step, risk, "not_delivered")
            except LedgerError as exc:
                evidence["ledger"] = _wal_detail(exc)  # the attempt stays open (D5)
        if isinstance(failure, _TakeoverError) or failure.code is ErrorCode.GATE_PENDING_USER:
            gate = "pending"
        elif failure.detail == "none_chosen" or (
            failure.detail == "killed" and not isinstance(failure, _KilledError)
        ):
            gate = "block"
        return self._failed(step, stage, evidence, gate=gate, dispatched=None, failure=failure)

    def _failed(  # noqa: PLR0913 - the step's state at the failure, nothing more
        self,
        step: Step,
        stage: _Stage,
        evidence: dict[str, Any],
        *,
        gate: GateToken,
        dispatched: ActionResult | None,
        failure: _StepFailureError | SteerError,
    ) -> StepResult:
        if isinstance(failure, _StepFailureError):
            evidence["detail"] = failure.detail
        else:
            evidence["detail"] = "observe_raised"
            if dispatched is not None:
                evidence["delivery"] = dispatched.delivery.value
        return self._finish(
            step,
            stage,
            evidence,
            gate=gate,
            action=dispatched,
            verify_code=None,
            label_kind=None,
            code=failure.code,
        )

    def _begin_step(self, windows_before: list[WindowLike] | None) -> None:
        self._windows_at_step = windows_before
        self._no_effect_facts = None
        self._proof = None
        self._url_proof = None
        self._scrolled = None
        self._secure_step = False
        self._sent_in_act = False
        self._binding = None
        self._candidates = None
        self._aliased = None
        self._twin_pick = None
        self._twin_rid = None
        self._counts_at_step = self._monitor.counts() if self._monitor is not None else {}

    def _input_evidence(self, evidence: dict[str, Any]) -> None:
        """Input during the step as counts of the person's events only (TK-20/25/37)."""
        if self._monitor is None:
            return
        now = self._monitor.counts()
        delta = {
            kind: now.get(kind, 0) - self._counts_at_step.get(kind, 0)
            for kind in ("user", "injected_other")
        }
        if any(delta.values()):
            evidence["user_input_during_step"] = True
            if not self._secure_step:
                evidence["input_counts"] = delta

    def _observe_fresh(self, evidence: dict[str, Any]) -> Snapshot:
        snapshot = self._observe()
        if not snapshot.stable:
            snapshot = self._observe()
        if not snapshot.stable:
            raise _StepFailureError(ErrorCode.UNSTABLE_UI, "unstable")
        evidence["observe_reused"] = False
        return snapshot

    def _before(self, risk: Risk, evidence: dict[str, Any]) -> Snapshot:
        if risk in ("R0", "R1") and self._last_after is not None:
            snapshot, stamp = self._last_after
            same_window = snapshot.hwnd == self.hwnd
            fresh = self._monotonic() - stamp <= REUSE_FRESHNESS_S
            if self._monitor is not None and self._monitor.last_user_input() >= stamp:
                evidence["reuse_skipped"] = "user_input"  # the person touched the screen (TK-16)
            elif same_window and snapshot.stable and fresh:
                evidence["observe_reused"] = True
                return snapshot
        return self._observe_fresh(evidence)

    def _before_at_ceiling(
        self, step: Step, key: str, risk: Risk, stage: _Stage, evidence: dict[str, Any]
    ) -> tuple[Snapshot, Risk]:
        """The before snapshot and the tier admission runs at: admission first, at the
        candidates' tier ceiling for a person target (a done or open record under this key is
        never bypassed by the row the person picks); a reused R0/R1 snapshot is no before for
        an R2/R3 step; right after a `type`, an R2/R3 step settles (5.11 [v2.44])."""
        before = stage.measure("observe", lambda: self._before(risk, evidence))
        risk = self._ceiling(step, key, before, risk)
        if risk in ("R2", "R3") and evidence.get("observe_reused"):
            before = stage.measure("observe", lambda: self._observe_fresh(evidence))
            risk = self._ceiling(step, key, before, effective_risk(step))
        if risk in ("R2", "R3") and self._settle_due():
            # after the ceiling, so a step raised to R2 settles too; policy time (OR-39)
            before = stage.measure("settle", partial(self._settled, before, evidence))
            risk = self._ceiling(step, key, before, effective_risk(step))
        return before, risk

    def _settle_due(self) -> bool:
        typed = self._typed_at
        if typed is not None and self._monotonic() - typed > SETTLE_AFTER_TYPE_CAP_S:
            self._typed_at = None  # the page has had the whole window: nothing to wait for
            return False
        return typed is not None

    def _settled(self, snapshot: Snapshot, evidence: dict[str, Any]) -> Snapshot:
        """Right after a `type`, an R2/R3 step's before waits until the page has kept its
        elements for QUIET_AFTER_TYPE_S (5.11 [v2.44]): a suggestion list re-rendering the field
        after the snapshot would otherwise change the screen during the ask or leave a stale
        composer handle. Once per typing, within SETTLE_AFTER_TYPE_CAP_S of it."""
        typed, self._typed_at = self._typed_at, None
        if typed is None:
            return snapshot
        ids = {e.runtime_id for e in snapshot.elements}
        quiet_from = self._monotonic()
        if self._last_after is not None:
            last, stamp = self._last_after
            if stamp >= typed and {e.runtime_id for e in last.elements} == ids:
                # unchanged since the type's own after snapshot: quiet since then
                quiet_from = stamp
        snapshot, _capped = self._quiet(
            snapshot, evidence, quiet_from, typed + SETTLE_AFTER_TYPE_CAP_S
        )
        evidence["settled_after_type"] = True
        return snapshot

    def _quiet(
        self, snapshot: Snapshot, evidence: dict[str, Any], quiet_from: float, deadline: float
    ) -> tuple[Snapshot, bool]:
        """The latest snapshot once the page has kept its elements for QUIET_AFTER_TYPE_S, and
        whether the deadline came first (`settle: capped`)."""
        ids = {e.runtime_id for e in snapshot.elements}
        # bounded by polls too: a clock that never moves must not spin (a stuck host clock)
        for _ in range(int(SETTLE_AFTER_TYPE_CAP_S / SETTLE_POLL_S) + 1):
            if self._monotonic() - quiet_from >= QUIET_AFTER_TYPE_S:
                return snapshot, False
            if self._monotonic() >= deadline:
                break
            self._sleep(SETTLE_POLL_S)
            observed = self._observe()
            if not observed.stable:
                quiet_from = self._monotonic()  # a tree changing during the walk is not quiet
                continue
            now = {e.runtime_id for e in observed.elements}
            if now != ids:
                ids, quiet_from = now, self._monotonic()
            snapshot = observed
        # the deadline, or the poll bound under a clock that never moves: never quiet
        evidence["settle"] = "capped"
        return snapshot, True

    def _resolved_target(
        self, step: Step, before: Snapshot, stage: _Stage, evidence: dict[str, Any]
    ) -> tuple[Snapshot, int]:
        """The target resolved and on screen: resolve (settling), the listbox admission on the
        resolved element (W3b, before any scroll), then the pre-gate scroll ([v2.55])."""
        before, element_id = self._resolve_settling(step, before, stage, evidence)
        _listbox_admission(step, before, element_id, self._live)
        return self._scroll_first(step, before, element_id, stage, evidence)

    def _scroll_first(
        self, step: Step, before: Snapshot, element_id: int, stage: _Stage, evidence: dict[str, Any]
    ) -> tuple[Snapshot, int]:
        """An offscreen target comes into view before the gate, the ask and the write-ahead
        (offscreen-lines packet, principal item 1, hunter OS-01/02/05/06/07): ScrollIntoView, a
        quiet look, the same element by RuntimeId; the before of record and the ask are that
        screen. Rows a lazy feed loads in the quiet look can push the target back below the
        fold (live, #184 re-review): it is scrolled once more, at most MAX_SCROLLS times. No
        ScrollItemPattern, or still offscreen after them, is refused with nothing sent
        (`offscreen_unreachable`)."""
        asked = before.element(element_id)
        if not asked.offscreen:
            return before, element_id
        if self._boundary(evidence):
            before, element_id = self._reobserve(
                step, before, evidence, asked=element_id, mismatch="screen_changed_during_wait"
            )
            evidence["restarted"] = True
            asked = before.element(element_id)
        for scrolls in range(1, MAX_SCROLLS + 1):
            if scrolls > 1 and self._boundary(evidence):
                # every scroll is a dispatch: kill, health and idle first (5.1 [v1.4])
                before, element_id = self._reobserve(
                    step, before, evidence, asked=element_id, mismatch="screen_changed_during_wait"
                )
                evidence["restarted"] = True
            result = self._semantic.execute(
                Action(verb="scroll_into_view", element_id=element_id), before
            )
            evidence["scrolled"] = result.delivery.value
            evidence["scrolls"] = scrolls
            if result.delivery is Delivery.NOT_DELIVERED and not result.skipped:
                raise _StepFailureError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "offscreen_unreachable")
            started = self._monotonic()
            fresh = stage.measure("observe", lambda: self._observe_fresh(evidence))
            fresh, capped = stage.measure(
                "settle",
                partial(self._quiet, fresh, evidence, started, started + SETTLE_AFTER_TYPE_CAP_S),
            )
            if capped:
                # a before taken mid-load would count the scroll's content as the step's effect
                raise _StepFailureError(ErrorCode.UNSTABLE_UI, "scroll_not_quiet")
            element_id = self._same_target(step, fresh, asked, "screen_changed_during_scroll")
            before = fresh
            if not fresh.element(element_id).offscreen:
                return fresh, element_id
        raise _StepFailureError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "offscreen_unreachable")

    def _resolve_settling(
        self, step: Step, before: Snapshot, stage: _Stage, evidence: dict[str, Any]
    ) -> tuple[Snapshot, int]:
        """Resolve the target; on an exact target's `no_match` settle and observe again, a bounded
        number of times (RN-18 generalised): a button re-created under an overlay is found on the
        next look, a truly absent one still fails `no_match`. Ambiguity never settles, and neither
        does a person target: its candidates are admitted by the ceiling (5.8), which a settled
        snapshot would bypass. The sleep is policy time (OR-39), counted under `settle`."""
        settled = 0
        person = step.target is not None and step.target.match == "person"
        while True:
            try:
                resolve = partial(self._resolve, step, before, focused=self._read_focus)
                return before, stage.measure("decide", resolve)
            except _StepFailureError as failure:
                if person or failure.detail != "no_match":
                    raise
                if settled >= SETTLE_RETRIES:
                    # the final no_match: a text field shown under another text role (gate item 1)
                    resolved = partial(self._resolve_alias, step, before, evidence)
                    return before, stage.measure("decide", resolved)
            settled += 1
            evidence["settled"] = settled
            stage.measure("settle", partial(self._sleep, SETTLE_S))
            before = stage.measure("observe", lambda: self._observe_fresh(evidence))

    def _resolve(
        self,
        step: Step,
        snapshot: Snapshot,
        *,
        focused: Callable[[], tuple[int, ...]] | None = None,
        asked: tuple[int, ...] | None = None,
    ) -> int:
        """The one element the target names; among twin fields (5.8 [v2.49]) a pick by the
        asked RuntimeId, the one on screen, or the focused one, else `ambiguous_target`."""
        target = step.target
        if target is None:  # enforced by Step.__post_init__; kept for the type checker
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "no_target")
        if target.match == "person":
            return self._resolve_person(step, target, snapshot)
        if target.element_id is not None:
            try:
                element = snapshot.element(target.element_id)
            except SteerError as exc:
                raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "no_match") from exc
            if element.role != target.role:
                # the tier reads the plan's role: an id of another kind is no match (5.13 [v2.17])
                raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "no_match")
            return target.element_id
        wanted = normalise_name(target.name).casefold()
        matches = [
            e
            for e in snapshot.elements
            if e.role == target.role and normalise_name(e.name).casefold() == wanted
        ]
        if not matches:
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "no_match")
        if len(matches) > 1:
            return self._twin(step, snapshot, matches, focused=focused, asked=asked)
        return matches[0].id

    def _twin(
        self,
        step: Step,
        snapshot: Snapshot,
        matches: list[Element],
        *,
        focused: Callable[[], tuple[int, ...]] | None,
        asked: tuple[int, ...] | None,
    ) -> int:
        """Twin fields (D-150 v2): (A) after an ask, a wait or a retry, the twin the step
        asked about, by RuntimeId; (B) focus/type at R0/R1, the one twin on screen; (C) an
        Enter submit's first resolve, the focused twin, refused when it is not the twin the
        verified type pinned. Anything else, and anything that is no twin set, is ambiguous."""
        if not twin_set(matches, snapshot, self._twin_value(snapshot)):
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "ambiguous_target")
        if asked is not None:
            if self._twin_pick is None:
                # a unique target that gained a copy during the ask or a wait (review of #164)
                raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "ambiguous_target")
            return self._picked([e for e in matches if e.runtime_id == asked], "asked")
        risk = effective_risk(step, element_ids=(matches[0].automation_id,))
        if step.verb in ("focus", "type", "choose") and risk in ("R0", "R1"):
            # a choose picks and pins its twin like a type (W4-11)
            return self._picked([e for e in matches if on_screen(e, snapshot)], "onscreen")
        if step.verb == "submit" and matches[0].role in TEXT_ROLES and focused is not None:
            current = focused()
            found = [e for e in matches if current and e.runtime_id == current]
            if len(found) != 1:
                self._act_evidence["twin"] = "none_focused"
                raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "ambiguous_target")
            pin = self._pins.get(pin_key(snapshot.hwnd, found[0].role, found[0].name))
            if pin is not None and pin != found[0].runtime_id:
                self._act_evidence["twin"] = "moved"
                raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "ambiguous_target")
            return self._picked(found, "focused")
        raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "ambiguous_target")

    def _picked(self, found: list[Element], how: str) -> int:
        if len(found) != 1:
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "ambiguous_target")
        if self._twin_pick is None:
            self._twin_pick = how
            self._act_evidence["twin"] = how
        self._twin_rid = found[0].runtime_id
        return found[0].id

    def _may_scroll(self) -> None:
        """A twin chosen as the one on screen that the act finds offscreen or clipped: the
        pick's premise broke, so stop, never scroll it into view (TW-10, principal blocking 1)."""
        if self._twin_pick == "onscreen":
            raise _StepFailureError(ErrorCode.FOCUS_LOST, ACT_MISMATCH)

    def _twin_value(self, snapshot: Snapshot) -> Callable[[Element], str | None]:
        """The shared twin-value reader over this session's live reads (read only when the
        chains differ)."""
        return twin_value_of(snapshot, self._live)

    def _read_focus(self) -> tuple[int, ...]:
        """The focused RuntimeId for an Enter submit's twin pick; unreadable is none."""
        if self._live is None:
            return ()
        try:
            return tuple(self._live.focused_runtime_id())
        except (OSError, SteerError):
            return ()

    def _resolve_again(
        self,
        step: Step,
        snapshot: Snapshot,
        evidence: dict[str, Any] | None = None,
        *,
        asked: tuple[int, ...] | None = None,
    ) -> int:
        """A later resolve in the same step (after an ask, a wait, a scroll or the act retry)
        goes the way the step first resolved (hunter CH-12); twins by the asked RuntimeId."""
        if self._aliased is not None:
            return self._resolve_alias(step, snapshot, {} if evidence is None else evidence)
        return self._resolve(step, snapshot, asked=asked)

    def _resolve_alias(self, step: Step, snapshot: Snapshot, evidence: dict[str, Any]) -> int:
        """An exact text target not on screen under its role: the one element of another text
        role with the same name, for `focus` and `type` at R0/R1 only; the model never changes
        the element (chooser-wiring gate items 1-3). The action key stays the plan's."""
        target = step.target
        if (
            target is None
            or target.element_id is not None
            or step.verb not in _ALIAS_VERBS
            or target.role not in ALIAS_ROLES
            or effective_risk(step) not in ("R0", "R1")
        ):
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "no_match")
        wanted = normalise_name(target.name).casefold()
        matches = [
            e
            for e in snapshot.elements
            if e.role in ALIAS_ROLES
            and e.role != target.role
            and (self._aliased is None or e.role == self._aliased)  # the role first resolved
            and not e.secure  # never a password field (hunter CH-04)
            and normalise_name(e.name).casefold() == wanted
        ]
        if not matches:
            evidence["alias"] = "none"
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "no_match")
        if len(matches) > 1:
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "ambiguous_target")
        element = matches[0]
        # the tier read on the element that will be touched, never only on the plan's words
        shown = replace(
            target, role=element.role, name=element.name, automation_id=element.automation_id
        )
        if _RISK_RANK[effective_risk(replace(step, target=shown))] > _RISK_RANK[step.risk]:
            raise _StepFailureError(ErrorCode.GATE_BLOCKED, "alias_risk")
        evidence["alias"] = element.role
        self._aliased = element.role
        return element.id

    def _resolve_person(self, step: Step, target: Target, snapshot: Snapshot) -> int:
        """A person target (5.8 [v1.5]): the bound row once bound; else the candidates, with
        the identity ask whenever more than one names the person or the only one is a
        lookalike (PT-01/06/08/26); never a silent pick."""
        evidence = self._act_evidence
        if self._binding is not None:
            found = rebind(self._binding, snapshot)
            if found is None:
                raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "candidate_gone")
            # same identity, current text: the confirm ask and the templates see what the
            # screen shows now (review item 2)
            self._binding = replace(bind(found), unique_name=self._binding.unique_name)
            return found.id
        cached = self._candidates
        found_all = (
            cached[1] if cached is not None and cached[0] == snapshot.id else pick(snapshot, target)
        )
        evidence["candidates"] = len(found_all)
        if not found_all:
            others = other_role_matches(snapshot, target)
            if others:
                evidence["other_role_matches"] = others
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "no_match")
        if too_many(found_all):
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "ambiguous_target")
        if len(found_all) == 1 and not lookalike_only(target.name, found_all[0].name):
            self._binding = bind(found_all[0], found_all)
            return found_all[0].id
        if self._monitor is not None:
            self._monitor.mark_ask_open()
        try:
            which = self._gate.which(step, found_all)
        finally:
            if self._monitor is not None:
                self._monitor.mark_ask_closed()
        evidence.update({k: v for k, v in which.evidence.items() if k != "detail"})
        if which.outcome == "chosen" and which.index is not None:
            element = found_all[which.index - 1]
            self._binding = bind(element, found_all)
            return element.id
        detail = str(which.evidence.get("detail", which.outcome))
        raise _StepFailureError(which.code or ErrorCode.GATE_PENDING_USER, detail)

    def _composer(self, step: Step, snapshot: Snapshot, *, own_twin: bool = True) -> int | None:
        """The composer a send template reads, resolved by role and name in this snapshot:
        element ids are positional and a plan must never carry one across steps (OR-06)."""
        role = step.params.get("composer_role")
        name = step.params.get("composer_name")
        if role is None or name is None:
            return None
        probe = Step(
            id=f"{step.id}.composer",
            subgoal=step.subgoal,
            verb="click",
            target=Target(role=role, name=name),
            risk="R0",
        )
        try:
            return self._resolve(probe, snapshot)
        except _StepFailureError as failure:
            own = (
                step.verb == "submit"
                and step.target is not None
                and step.target.role == role
                and step.target.role in TEXT_ROLES
                and normalise_name(step.target.name).casefold() == normalise_name(name).casefold()
            )
            if (
                failure.detail != "ambiguous_target"
                or not own
                or not own_twin
                or self._twin_rid is None
            ):
                raise
            # an Enter submit's composer is its own target: the twin it resolved (TW-06/07)
            twins = [e for e in snapshot.elements if e.role == role and _same_name(e, name)]
            found = [e for e in twins if e.runtime_id == self._twin_rid]
            if not twin_set(twins, snapshot, self._twin_value(snapshot)) or len(found) != 1:
                raise
            return found[0].id

    def _admission(self, step: Step, key: str, risk: Risk, before: Snapshot) -> str | None:
        """None when the key is fresh, "done" when nothing may run, else the previous attempt's
        token the gate shows (an outcome, a phase, or "unknown")."""
        if risk not in ("R2", "R3"):
            return None
        try:
            admission = self._ledger.admit(key)
            record = self._ledger.read_ahead(step)
        except LedgerError:
            return "unknown"
        if admission == "done":
            return "done"
        if record is not None and self._ledger.outcome_of(key) is None:
            return self._resume(step, record, before)
        return None if admission == "fresh" else (self._ledger.outcome_of(key) or "unknown")

    def _resume(self, step: Step, record: WriteAheadRecord, before: Snapshot) -> str:
        """A crash between the record and the outcome (OR-01): done when the effect is already
        there relative to the persisted before, else the phase the gate shows."""
        if record.chosen is not None:
            self._binding = Binding.from_record(record.chosen)  # the row the attempt chose
        try:
            # a persisted snapshot never takes the step's live twin identity (review of #164)
            composer_id = self._composer(step, record.before, own_twin=False)
        except _StepFailureError:
            return record.phase
        expect = self._gate.predicate_for(step, composer_id=composer_id, bound=self._binding)
        if expect is not None and self._effect_present(
            expect, record.before, before, step, record.url_before
        ):
            self._ledger.record_outcome(step, "done_already")
            return "done"
        return record.phase

    def _effect_present(
        self,
        predicate: Predicate,
        persisted: Snapshot,
        now: Snapshot,
        step: Step,
        url_before: UrlBefore | None = None,
    ) -> bool:
        if persisted.hwnd != now.hwnd or (persisted.pid and now.pid and persisted.pid != now.pid):
            return False
        risk = effective_risk(step, element_ids=self._step_ids(step, persisted))
        if url_before is not None and self._url_eligible(step, predicate, risk, persisted):
            # the same proof as a live verify, on the record's digest (#148 design D item 5)
            after, text = self._read_url(), step.params.get("text")
            if (
                after is not None
                and isinstance(text, str)
                and url_before.counts(after, text)
                and page_loaded(
                    _document_names(now),
                    text,
                    before=_document_names(persisted),
                    url=after,
                    document_url=self._read_document_url(),
                    url_before=url_before,
                )
            ):
                return True
        structural = _structural_part(predicate)
        if structural is None:
            return False
        try:
            verdict = evaluate(structural, persisted, now)
        except (SteerError, ValueError):
            return False
        if verdict.holds and "renamed" in verdict.evidence:
            # the same rule as a live verify, so a crash after Enter resumes the same way
            risk = effective_risk(step, element_ids=self._step_ids(step, persisted))
            return self._rename_counts(step, predicate, risk, persisted)
        return verdict.holds and verdict.level != 3

    def _url_eligible(
        self, step: Step, expect: Predicate | None, risk: Risk, before: Snapshot
    ) -> bool:
        """A search submit (`url_query_eligible`) in a chrome.exe window this session can read
        the address bar of (#148 design D item 3)."""
        if self._address_bar is None or self._identify is None:
            return False
        identity = self._identify(self.hwnd)
        if identity is None or str(identity.get("image", "")).lower() != "chrome.exe":
            return False
        bound = self._binding.full_name if self._binding is not None else None
        deny = deny_tier(step, bound, self._step_ids(step, before))
        return url_query_eligible(step, expect, risk=risk, deny=deny, goal=self._goal)

    def _read_url(self) -> str | None:
        """The task window's address bar; a failed read is no URL (the step stays unverified
        with `no_url`), never a pass."""
        if self._address_bar is None:
            return None
        try:
            return self._address_bar(self.hwnd)
        except (OSError, SteerError):
            return None

    def _read_document_url(self) -> str | None:
        """The top Document's URL; a failed read is none (the words rule alone decides)."""
        if self._document_url is None:
            return None
        try:
            return self._document_url(self.hwnd)
        except (OSError, SteerError):
            return None

    def _url_before_of(self, step: Step) -> UrlBefore | None:
        """The before URL of record: a retry keeps the first attempt's (as the before snapshot
        of record does); else read now, before the Enter."""
        prior = self._ledger.read_ahead(step)
        if prior is not None:
            return prior.url_before
        value, text = self._read_url(), step.params.get("text")
        return None if value is None or not isinstance(text, str) else UrlBefore.of(value, text)

    def _url_counts(
        self, step: Step, before: Snapshot, page: Snapshot, after: str | None
    ) -> tuple[bool, dict[str, Any]]:
        """The address bar carries the words and a new page loaded for them (`page_loaded`;
        `page` is the verify's last observation, `before` the before of record, `after` the
        address bar read after the verify's deadline)."""
        text = step.params.get("text")
        if self._url_before is None or after is None or not isinstance(text, str):
            return False, {"detail": "no_url"}
        if not self._url_before.counts(after, text):
            return False, {}
        if not page_loaded(
            _document_names(page),
            text,
            before=_document_names(before),
            url=after,
            document_url=self._read_document_url(),
            url_before=self._url_before,
        ):
            return False, {"detail": "no_load"}
        return True, {"reason": "url_query"}

    def _no_effect(  # noqa: PLR0913 - the step's state at the verify deadline
        self,
        step: Step,
        verdict: Verdict,
        action: ActionResult,
        *,
        evidence: Mapping[str, Any],
        before: Snapshot,
        after: Snapshot,
        url_now: str | None,
        windows_before: list[WindowLike] | None,
    ) -> bool:
        """An Enter that provably changed nothing (packet no-effect-submit-2026-11-10; [v2.64]
        npm1: a backdrop submit button swallows the Enter while the dropdown is open). Only an
        Enter on a text-role composer (NE-07; a Button submit that misses stays uncertain),
        delivered (NE-05: never uncertain), whose verify missed at its deadline (`no_match`,
        `not_new`, `not_replaced`), and the five facts of `_facts_hold`, read now, after that
        deadline and never earlier: a slow page that lands while the verify polls is the
        verify's to catch. Any fact missing keeps `unverified_effect`. The facts are kept for
        the re-read before the replan's planner call (`no_effect_holds`, NE-08). The evidence
        says unchanged for the verify's deadline, never proven: a page can still act later."""
        self._no_effect_facts = None
        if evidence.get("submit") != "enter" or not self._sent_in_act:
            return False
        if action.delivery not in _DELIVERED:
            return False
        missed = verdict.code is ErrorCode.VERIFY_TIMEOUT
        if not missed or verdict.evidence.get("reason") not in _MISSES:
            return False
        text = step.params.get("text")
        if self._url_before is None or not isinstance(text, str):
            return False
        facts = _NoEffectFacts(
            step=step, text=text, url_before=self._url_before, before=before, windows=windows_before
        )
        if not self._facts_hold(facts, after, url_now):
            return False
        self._no_effect_facts = facts
        return True

    def _facts_hold(self, facts: _NoEffectFacts, now: Snapshot, url_now: str | None) -> bool:
        """The five facts, each required: (1) the address bar equals the before URL of record
        (its digest), both readable; (2) the top Document keeps its RuntimeId and name (a
        same-URL reload changes the RuntimeId, NE-13); (3)
        the composer, resolved in `now` by role and exact name (exactly one: twins are a
        missing fact), still holds exactly the approved text, read through `now`'s handle,
        never the before-of-record's (NE-04: a re-rendered input raises on the stale handle,
        and the fact is the text, not the element); (4) no element with a new RuntimeId
        outside the composer's nearest container (NE-01: an instant-search page or an
        in-place re-render is an effect; a removal, npm's dropdown closing, is not); (5) the
        top-level window list is the step's (a results tab elsewhere is an effect)."""
        composer = self._held_composer(facts, now)
        return (
            url_now is not None
            and UrlBefore.of(url_now, facts.text) == facts.url_before
            and _same_top_document(facts.before, now)
            and composer is not None
            and not _new_outside(facts.before, now, composer)
            and self._same_windows(facts.windows)
        )

    def _held_composer(self, facts: _NoEffectFacts, now: Snapshot) -> int | None:
        """The one composer of `now` that still holds the approved text, read through `now`'s
        handle (fact 3), else None."""
        target = _composer_target(facts.step)
        composer = None if target is None else _one_field(now, *target)
        if composer is None:
            return None
        try:
            self._check_composer(facts.step, now, composer)
        except _StepFailureError:
            return None
        return composer

    def _same_windows(self, windows_before: list[WindowLike] | None) -> bool:
        """The top-level windows now are the step's (fact 5); no list is a missing fact."""
        if self._windows is None or windows_before is None:
            return False
        return {w.hwnd for w in self._windows()} == {w.hwnd for w in windows_before}

    def no_effect_holds(self) -> bool:
        """The last step's `no_effect` facts read again, on a fresh look, before the replan's
        planner call (NE-08); False when nothing was judged `no_effect` or anything moved
        since, which the runner ends `unverified_effect`."""
        facts = self._no_effect_facts
        if facts is None:
            return False
        try:
            now = self._observe()
        except SteerError:
            return False
        return self._facts_hold(facts, now, self._read_url())

    def _pin(self, target: Target, level: int) -> None:
        """A verified type of a twin pins it for conditions; any other verified type of the
        same field replaces the pin (D-150 v2 item 4)."""
        key = pin_key(self.hwnd, target.role, target.name)
        if self._twin_pick is not None and self._twin_rid is not None and level == 3:
            self._pins[key] = self._twin_rid
        else:
            self._pins.pop(key, None)

    def _rename_counts(self, step: Step, expect: Predicate, risk: Risk, before: Snapshot) -> bool:
        bound = self._binding.full_name if self._binding is not None else None
        deny = deny_tier(step, bound, self._step_ids(step, before))
        return rename_counts(step, expect, risk=risk, deny=deny, goal=self._goal)

    def _step_ids(self, step: Step, snapshot: Snapshot) -> tuple[str, ...]:
        """The automation ids of the elements the step's target resolves to (#84)."""
        return () if step.target is None else self._resolvable_ids(step.target, snapshot)

    def _admitted(
        self, key: str, previous: str | None, gate_result: GateResult
    ) -> tuple[GateDecision, Predicate]:
        decision = gate_result.decision
        expect = gate_result.expect
        if decision is None or expect is None:
            raise _StepFailureError(ErrorCode.GATE_BLOCKED, "no_decision")
        if previous is not None and self._ledger.admit(key, decision) != "fresh":
            raise _StepFailureError(ErrorCode.GATE_BLOCKED, "needs_confirm")
        return decision, expect

    def _act(
        self, step: Step, risk: Risk, before: Snapshot, element_id: int
    ) -> tuple[ActionResult, Snapshot, int]:
        path, verb = executor_verb(step.verb)
        if step.path is not None:
            path = step.path
        chord: str | None = None
        if step.verb == "submit" and before.element(element_id).role in TEXT_ROLES:
            # a composer submits with Enter: a physical press on the focused field
            path, verb, chord = "physical", "press", "enter"
            self._act_evidence["submit"] = "enter"
        executor = self._physical if path == "physical" else self._semantic
        retries = 0
        scrolled = False
        # the element approved or observed: every re-resolve below must find it again (#89)
        asked = before.element(element_id)
        while True:
            self._still_the_window()  # a gone window never leaves an acting record
            if risk in ("R2", "R3"):
                try:
                    self._ledger.mark(step, "acting")
                except LedgerError as exc:
                    raise _StepFailureError(ErrorCode.GATE_BLOCKED, _wal_detail(exc)) from exc
            before, element_id = self._waited_in_act(step, before, element_id)
            self._still_the_window()
            if self._monitor is not None:
                # input during the step is what lands after this point (D7)
                self._counts_at_step = self._monitor.counts()
            option = step.params.get(OPTION_PARAM) if verb == "choose" else None
            action = self._build(path, verb, element_id, step.text, chord, option=option)
            result, before, element_id = self._dispatch(step, executor, action, before)
            if result.delivery is not Delivery.NOT_DELIVERED or (
                result.steps and not result.skipped
            ):
                self._sent_in_act = True  # something may have reached the app (a skip never)
            if result.code in (ErrorCode.GATE_BLOCKED, ErrorCode.GATE_PENDING_USER):
                return result, before, retries
            if result.delivery is not Delivery.NOT_DELIVERED:
                return result, before, retries
            detail = result.evidence.get("detail")
            if detail in ("offscreen", "clipped") and not scrolled and risk in ("R0", "R1"):
                # at R2/R3 never a scroll after the write-ahead and the before of record (OS-01r)
                scrolled = True
                before, element_id = self._scrolled_in_act(step, before, element_id, asked)
                continue
            if result.code is ErrorCode.ELEMENT_NOT_FOUND and retries == 0 and not result.steps:
                retries += 1
                before = self._observe(stable_window=1)
                element_id = self._same_target(step, before, asked, ACT_MISMATCH)
                continue
            return result, before, retries

    def _still_shown(  # noqa: PLR0913 - the step's state after the ask, passed once
        self,
        step: Step,
        before: Snapshot,
        element_id: int,
        stage: _Stage,
        evidence: dict[str, Any],
        *,
        asked: str,
        risk: Risk,
    ) -> tuple[Snapshot, int]:
        """After an ask or a wait, the target the fresh look found: approved on screen and
        scrolled away since is never acted on (hunter OS-01r on #184); after a wait at R0/R1
        it comes into view again through the pre-gate scroll."""
        if not before.element(element_id).offscreen:
            return before, element_id
        if asked == "confirm" or risk in ("R2", "R3"):
            raise _StepFailureError(ErrorCode.FOCUS_LOST, "offscreen_after_ask")
        return self._scroll_first(step, before, element_id, stage, evidence)

    def _waited_in_act(self, step: Step, before: Snapshot, element_id: int) -> tuple[Snapshot, int]:
        """A wait inside `_act` ended after the observe too: same screen, fresh target (TK-03);
        a target offscreen now would need a scroll after the before of record, so it stops."""
        if not self._boundary(self._act_evidence):
            return before, element_id
        before, element_id = self._reobserve(
            step,
            before,
            self._act_evidence,
            asked=element_id,
            mismatch="screen_changed_during_wait",
        )
        self._act_evidence["restarted"] = True
        if before.element(element_id).offscreen:
            raise _StepFailureError(ErrorCode.FOCUS_LOST, "offscreen_after_wait")
        return before, element_id

    def _scrolled_in_act(
        self, step: Step, before: Snapshot, element_id: int, asked: Element
    ) -> tuple[Snapshot, int]:
        """An R0/R1 physical refusal for `offscreen` / `clipped`: one scroll, then the pre-gate
        scroll's quiet look, so T1's before is never a look taken mid-load (OS-01r)."""
        self._may_scroll()
        self._boundary(self._act_evidence)  # the scroll is a dispatch too
        scroll_result = self._semantic.execute(
            Action(verb="scroll_into_view", element_id=element_id), before
        )
        self._scrolled = scroll_result.delivery.value
        started = self._monotonic()
        before, capped = self._quiet(
            self._observe(stable_window=1),
            self._act_evidence,
            started,
            started + SETTLE_AFTER_TYPE_CAP_S,
        )
        if capped:
            raise _StepFailureError(ErrorCode.UNSTABLE_UI, "scroll_not_quiet")
        return before, self._same_target(step, before, asked, ACT_MISMATCH)

    def _dispatch(
        self, step: Step, executor: ExecutorLike, action: Any, before: Snapshot
    ) -> tuple[ActionResult, Snapshot, int]:
        """One execution. A send or submit first proves the composer shows the approved text
        (OR-31): Enter on the composer itself, and a Send button through the composer its
        template names, so a failed type or a page that rewrote the field after the yes never
        gets the click (Layer B positive control, 2026-11-07). A focus click refused for a
        transparent overlay under the pointer (`hit_test_miss`, nothing sent) falls back to UIA
        SetFocus on a fresh look if the boundary waited (TK-03), verified like any focus step
        (5.9 [v1.7])."""
        element_id: int = action.element_id
        if getattr(action, "chord", None) == "enter":
            self._check_composer(step, before, element_id)
        elif step.verb in ("send", "submit"):
            composer = self._prove_composer(step, before)
            if (
                step.verb == "submit"
                and composer is not None
                and not _shares_form(before, element_id, composer)
            ):
                # the one Button beside the field, never a Search button elsewhere (NE-10)
                raise _StepFailureError(ErrorCode.GATE_BLOCKED, "button_not_beside")
        result = executor.execute(action, before)
        if action.verb == "type" and result.delivery is not Delivery.NOT_DELIVERED:
            self._typed_at = self._monotonic()
        refused = result.delivery is Delivery.NOT_DELIVERED and not result.steps
        detail = result.evidence.get("detail")
        if (
            refused
            and executor is self._semantic
            and action.verb == "click"
            and detail == "no_InvokePattern"
            and physical_click_target(before, before.element(element_id))
        ):
            # no Invoke on a plain control: the hit-tested physical click, once (5.9 [v2.50])
            before, element_id = self._fallback_look(step, before, element_id)
            if not physical_click_target(before, before.element(element_id)):
                return result, before, element_id  # an acting child appeared in the wait
            self._act_evidence["click_fallback"] = "physical"
            self._act_evidence["semantic_detail"] = "no_InvokePattern"
            physical = PhysicalAction(verb="click", element_id=element_id)
            return self._physical.execute(physical, before), before, element_id
        if not (step.verb == "focus" and refused and detail == "hit_test_miss"):
            return result, before, element_id
        before, element_id = self._fallback_look(step, before, element_id)
        result = self._semantic.execute(Action(verb="focus", element_id=element_id), before)
        self._act_evidence["focus_fallback"] = "set_focus"
        return result, before, element_id

    def _fallback_look(self, step: Step, before: Snapshot, element_id: int) -> tuple[Snapshot, int]:
        """Before a fallback dispatch: a boundary wait restarts from a fresh look at the same
        element (TK-03), the window must still be the task's, the wait's input is not the
        step's (D7), and a send or submit proves its composer again (OR-31)."""
        if self._boundary(self._act_evidence):
            before, element_id = self._reobserve(
                step,
                before,
                self._act_evidence,
                asked=element_id,
                mismatch="screen_changed_during_wait",
            )
            self._act_evidence["restarted"] = True
            if self._monitor is not None:
                self._counts_at_step = self._monitor.counts()
            if step.verb in ("send", "submit"):
                self._prove_composer(step, before)
        self._still_the_window()
        return before, element_id

    def _prove_composer(self, step: Step, before: Snapshot) -> int | None:
        """A send or submit through a button proves its composer shows the approved text;
        text that cannot be checked is never sent, and a plain form button (no text, no
        composer) has nothing to check (principal review of #112). The composer's id, or
        None for a plain form button."""
        composer = self._composer(step, before)
        if composer is not None:
            self._check_composer(step, before, composer)
        elif step.verb == "send" or step.params.get("text") or step.text:
            raise _StepFailureError(ErrorCode.GATE_BLOCKED, "composer_unknown")
        return composer

    def _check_composer(self, step: Step, before: Snapshot, element_id: int) -> None:
        """Enter submits whatever the field shows: it must be the text the person approved
        (SE-02). The field's text never enters the evidence."""
        text = step.params.get("text", step.text)
        if not text:
            raise _StepFailureError(ErrorCode.GATE_BLOCKED, "submit_text_missing")
        if self._live is None:
            raise _StepFailureError(ErrorCode.GATE_BLOCKED, "composer_unreadable")
        try:
            value = self._live.value(before.handle(element_id))
        except SteerError as exc:
            raise _StepFailureError(ErrorCode.GATE_BLOCKED, "composer_unreadable") from exc
        role = before.element(element_id).role
        if value is None or field_text(value, role) != normalise_newlines(text):
            raise _StepFailureError(ErrorCode.GATE_BLOCKED, "composer_differs")

    @staticmethod
    def _build(  # noqa: PLR0913 - the executor action's fields, each from the step
        path: str,
        verb: str,
        element_id: int,
        text: str | None,
        chord: str | None = None,
        *,
        option: str | None = None,
    ) -> Any:
        if path == "physical":
            return PhysicalAction(verb=verb, element_id=element_id, text=text, chord=chord)
        return Action(verb=verb, element_id=element_id, option=option)

    def _verify(
        self, expect: Predicate, before: Snapshot, windows_before: Sequence[WindowLike] | None
    ) -> tuple[Any, Snapshot]:
        decided: list[Snapshot] = [self._observe(stable_window=1)]

        def observe(**kwargs: Any) -> Snapshot:
            decided.append(self._observe(**kwargs))
            return decided[-1]

        verdict = verify(
            expect,
            before,
            decided[0],
            observe,
            live=self._live,
            windows=self._windows() if self._windows else None,
            windows_before=windows_before,
            pinned=self._pins,
            deadline_s=_verify_deadline(expect, self._verify_deadline_s),
            monotonic=self._monotonic,
            sleep=self._sleep,
        )
        after = next((s for s in decided if s.id == verdict.after_id), decided[-1])
        self._proof = (expect, verdict) if verdict.holds else None
        return verdict, after

    def _remember(self, before: Snapshot, after: Snapshot) -> None:
        self._last_pair = (before, after)
        if after.stable:
            self._last_after = (after, self._monotonic())

    @staticmethod
    def _outcome(holds: bool, action: ActionResult, reason: object) -> str:
        if holds:
            return "delivered_verified"
        if action.delivery is Delivery.NOT_DELIVERED:
            return "not_delivered"
        if reason in NO_LABEL_REASONS:
            return "no_label"
        return (
            action.delivery.value if action.delivery is Delivery.DELIVERY_UNCERTAIN else str(reason)
        )

    def _close(self, step: Step, risk: Risk, outcome: str) -> None:
        if risk in ("R2", "R3"):
            self._ledger.record_outcome(step, outcome)

    # ---- results ----------------------------------------------------------------------------

    def _state_holds(
        self, step: Step, before: Snapshot, stage: _Stage, evidence: dict[str, Any]
    ) -> None:
        """A widget verb's done key proves the state was set, not that it holds now: the page
        or the person may have changed it since (WV-12). Re-read it (a selection through
        IsSelected, a box through its ToggleState, W3-05, a disclosure Button through its
        ExpandCollapseState, W5, a choose's ComboBox through its Value by the choose's
        comparator, W4-07); a changed state stops, never a second Toggle, Select, Expand,
        Collapse or choose under a done key."""
        before, element_id = self._resolve_settling(step, before, stage, evidence)
        try:
            holds = None if self._live is None else self._holds_live(step, before, element_id)
        except SteerError as exc:
            raise _StepFailureError(ErrorCode.VERIFY_FAILED, "state_unreadable") from exc
        if holds is not True:
            raise _StepFailureError(ErrorCode.VERIFY_FAILED, "state_changed_since_done")

    def _holds_live(self, step: Step, before: Snapshot, element_id: int) -> bool | None:
        """The widget verb's own state read live: None when it cannot be read."""
        if self._live is None:
            return None
        handle = before.handle(element_id)
        if step.verb != "choose":
            return reads_wanted(self._live, handle, STATE_WANTED[step.verb])
        raw = self._live.value(handle)
        option = step.params.get(OPTION_PARAM, "")
        return None if raw is None else option_key(str(raw)) == option_key(option)

    def _done_already(
        self, step: Step, key: str, stage: _Stage, evidence: dict[str, Any]
    ) -> StepResult:
        # a twin keyed like an earlier step proves that step's effect, not its own subgoal
        closed_by = self._ledger.closed_by(key)
        evidence["done_by"] = "this_step" if closed_by in (None, step.id) else "another_step"
        return self._finish(
            step,
            stage,
            evidence,
            gate="done_already",
            action=None,
            verify_code=None,
            label_kind=None,
            ok=True,
        )

    def _gate_stopped(
        self, step: Step, gate_result: GateResult, stage: _Stage, evidence: dict[str, Any]
    ) -> StepResult:
        evidence.update(gate_result.evidence)
        return self._finish(
            step,
            stage,
            evidence,
            gate=gate_result.outcome,
            action=None,
            verify_code=None,
            label_kind=None,
            code=gate_result.code,
        )

    def _finish(  # noqa: PLR0913 - one keyword-only constructor for every exit of a step
        self,
        step: Step,
        stage: _Stage,
        evidence: dict[str, Any],
        *,
        gate: Any,
        action: ActionResult | None = None,
        verify_code: ErrorCode | None = None,
        label_kind: Literal["t1", "user"] | None = None,
        ok: bool = False,
        code: ErrorCode | None = None,
    ) -> StepResult:
        self._input_evidence(evidence)
        result = StepResult(
            step_id=step.id,
            ok=ok,
            gate=gate,
            action=action,
            verify_code=None if ok else verify_code,
            label_kind=label_kind,
            latency_ms=dict(stage.totals),
            evidence=evidence,
            code=None if ok else code,
        )
        self._trace.write({"kind": "step", "task_id": self.task_id, **result.to_dict()})
        return result

    # ---- a whole plan --------------------------------------------------------------------------

    def _judge(self, plan: Plan) -> None:
        """One goal-match call per plan, before any step: verdict tokens to the trace, never
        the goal, names or texts; a continuation is judged with the steps already done."""
        if self._preset is not None and self._preset[0] == plan:
            self._verdicts = dict(self._preset[1])
            self._judge_failure = None
            self._trace.write(
                {
                    "kind": "goal_match",
                    "task_id": self.task_id,
                    "verdicts": dict(self._verdicts),
                    "failure": None,
                    "model": "cache",
                    "latency_ms": 0.0,
                }
            )
            return
        if self._goal_matcher is None:
            self._verdicts = None
            self._trace.write({"kind": "goal_match", "task_id": self.task_id, "goal_match": "off"})
            return
        judgement = self._goal_matcher.judge(plan.goal, plan.steps, self._done_steps)
        self._judge_failure = judgement.failure
        judged = [s for s in plan.steps if s.verb != "open"]
        self._verdicts = {s.id: judgement.verdict(s.id) for s in judged}
        self._trace.write(
            {
                "kind": "goal_match",
                "task_id": self.task_id,
                "verdicts": dict(self._verdicts),
                "failure": judgement.failure,
                "model": judgement.model,
                "latency_ms": round(judgement.latency_ms, 1),
            }
        )

    def preset(self, plan: Plan, verdicts: Mapping[str, str]) -> None:
        """A trusted cache entry's verdicts stand for this exact plan only; any other plan (a
        continuation) is judged live (hunter PC-12)."""
        self._preset = (plan, dict(verdicts))

    @property
    def judged(self) -> tuple[dict[str, str], str | None]:
        """The last plan's verdict tokens and its judgement's failure token (cache write)."""
        return dict(self._verdicts or {}), self._judge_failure

    def _note_done(self, step: Step, result: StepResult) -> None:
        if result.ok:
            self._done_steps.append(step)

    def _verdict(self, step: Step) -> str | None:
        """A step's verdict; a step the plan's judgement never saw is `unknown` (it asks), and
        an `open` is the code's own (`open_tier`)."""
        if self._verdicts is None or step.verb == "open":
            return None
        return self._verdicts.get(step.id, "unknown")

    def run_task(self, plan: Plan, *, continuation: bool = False) -> TaskResult:
        """Run one plan. A continuation (plan n>1 of a replanned task) ticks no subgoal at
        start: its conditions were written from the screen it starts on (hunter RP-01)."""
        self._goal = plan.goal
        self._pins.clear()  # a new plan pins afresh (D-150 v2 item 4)
        self._content_landmarks = _content_landmarks(plan)
        self._last_after = None  # nothing observed for another plan is reused (RP-35/47)
        self._last_pair = None
        self._trace.write(
            {
                "kind": "task",
                "task_id": self.task_id,
                "steps": len(plan.steps),
                "subgoals": len(plan.subgoals),
            }
        )
        self._judge(plan)
        results: list[StepResult] = []
        if not continuation:
            self._verbs_complete = True
        elif not self._verbs_complete:
            earlier = self._ledger.earlier_verbs()
            if earlier is not None:
                for subgoal, names in earlier.items():
                    self._verbs.setdefault(subgoal, set()).update(names)
                self._verbs_complete = True
        for subgoal, listed in subgoal_verbs(plan).items():
            self._verbs.setdefault(subgoal, set()).update(listed)
        try:
            self._rebind_ref(continuation=continuation)
        except _StepFailureError as failure:
            return self._ended_at_start(failure.code, failure.detail)
        try:
            start = self._observe()
        except SteerError as failure:
            # the window is not there to start on (RN-14): a result, not a traceback
            self._trace.write(
                {
                    "kind": "task_end",
                    "task_id": self.task_id,
                    "status": "stopped",
                    "detail": "observe_raised",
                }
            )
            return TaskResult(
                status="stopped",
                results=results,
                ticked=set(),
                code=failure.code,
                evidence={"detail": "observe_raised"},
            )
        link = self._ledger.window_ref
        self._step_refs = _plan_refs(plan, link)
        opening = {s.subgoal for s in plan.steps if s.verb == "open"}
        elsewhere = {s.subgoal for s in plan.steps if self._step_refs[s.id] != link}
        # an open's subgoal is reached in the window it opens, and a subgoal acted on in
        # another window is never judged on this one (SW-02)
        future = _future_subgoals(plan)
        ticked = {
            s.id
            for s in plan.subgoals
            if not continuation
            and s.id not in opening
            and s.id not in elsewhere
            and s.id not in future
            # a value or a box's state is a step's doing (FV-01/03, W2-4)
            and not field_values(s.condition)
            and not check_states(s.condition)
            and self._holds_now(s.condition, start)
        }
        for subgoal_id in sorted(ticked):
            self._trace_tick(subgoal_id, "start")
        outcome = self._run_steps(plan, ticked, results)
        if outcome is None and ticked >= {s.id for s in plan.subgoals}:
            outcome = self._reset_before_done(plan, ticked)
        if outcome is not None:
            stopped, code, detail = outcome
            evidence = {"detail": detail}
            self._trace.write(
                {"kind": "task_end", "task_id": self.task_id, "status": stopped, "detail": detail}
            )
            return TaskResult(
                status=stopped, results=results, ticked=ticked, code=code, evidence=evidence
            )
        status: Literal["done", "stopped"] = (
            "done" if ticked >= {s.id for s in plan.subgoals} else "stopped"
        )
        evidence = {} if status == "done" else {"detail": "unfinished"}
        self._trace.write(
            {"kind": "task_end", "task_id": self.task_id, "status": status, **evidence}
        )
        return TaskResult(
            status=status,
            results=results,
            ticked=ticked,
            code=None if status == "done" else ErrorCode.VERIFY_FAILED,
            evidence=evidence,
        )

    def _ended_at_start(self, code: ErrorCode, detail: str) -> TaskResult:
        self._trace.write(
            {"kind": "task_end", "task_id": self.task_id, "status": "stopped", "detail": detail}
        )
        return TaskResult(
            status="stopped", results=[], ticked=set(), code=code, evidence={"detail": detail}
        )

    def _run_steps(
        self, plan: Plan, ticked: set[str], results: list[StepResult]
    ) -> tuple[Literal["stopped", "paused", "blocked"], ErrorCode, str] | None:
        strikes = 0
        for index, step in enumerate(plan.steps):
            if step.subgoal in ticked:
                continue
            refused = self._refused_start(plan, index, ticked)
            if refused is not None:
                return refused
            self._steps_run += 1
            result = self._run_planned(plan, index, ticked)
            results.append(result)
            self._note_done(step, result)
            self._untick(ticked, self._reset)
            stop = (
                self._after_open(plan, step, results, ticked)
                if step.verb == "open"
                else _stop_for(result) or _state_stop(step, result)
            )
            if stop is not None:
                return stop
            if step.verb == "open":
                strikes = 0
                continue
            if result.gate == "done_already":
                # the earlier verified attempt (or the persisted before) proved the effect
                if result.evidence.get("done_by") != "another_step" and _value_ready(
                    plan, step, result, results
                ):
                    ticked.add(step.subgoal)
                    self._trace_tick(step.subgoal, "done_already")
                strikes = 0
                continue
            if result.ok and self._last_pair is not None:
                before, after = self._last_pair
                subgoal = next(s for s in plan.subgoals if s.id == step.subgoal)
                verdict = (
                    self._condition_verdict(subgoal.condition, before, after)
                    if _value_ready(plan, step, result, results)
                    else None
                )
                if verdict is not None and verdict.holds:
                    ticked.add(subgoal.id)
                    alias = verdict.evidence.get("alias")
                    role = alias if isinstance(alias, str) else None
                    reason = verdict.evidence.get("reason")
                    by = reason if reason in ("url_query", "url_query_title") else "condition"
                    self._trace_tick(subgoal.id, by, alias=role)
                    strikes = 0
                    continue
            if result.ok:
                strikes = 0
            elif self._last_pair is None or _same_screen(*self._last_pair):
                strikes += 1
            if strikes >= SCREEN_GUARD_STRIKES:
                return "stopped", ErrorCode.BUDGET_EXCEEDED, "loop"
        return None

    def _run_planned(self, plan: Plan, index: int, ticked: set[str]) -> StepResult:
        """One plan step, knowing whether an earlier subgoal is unmet (#190 review item 1)."""
        self._earlier_unmet = _earlier_unticked(plan, index, ticked)
        self._ticks, self._reset = (plan, ticked), set()
        try:
            return self.run_step(plan.steps[index])
        finally:
            self._earlier_unmet = False
            self._ticks = None

    # ---- the ticked values and states, read again (W4-04) ----------------------------------------

    def _reread_ticks(self, before: Snapshot) -> None:
        """Before an R2/R3 step: the ticked values and states of this window, read again on the
        step's own before (live reads, no observe). A contradiction stops the step before the
        gate: nothing dispatched, nothing asked (the principal's W4 v3 correction 2)."""
        if self._ticks is None:
            return
        plan, ticked = self._ticks
        self._reset = self._contradicted(plan, ticked, before)
        if self._reset:
            raise _StepFailureError(ErrorCode.VERIFY_FAILED, "reset")

    def _reset_before_done(
        self, plan: Plan, ticked: set[str]
    ) -> tuple[Literal["stopped"], ErrorCode, str] | None:
        """Before `done`: the same re-read on the last pair's after, never a new observe; none
        taken (every step done already) reads nothing."""
        if self._last_pair is None:
            return None
        reset = self._contradicted(plan, ticked, self._last_pair[1])
        if not reset:
            return None
        self._untick(ticked, reset)
        return "stopped", ErrorCode.VERIFY_FAILED, "reset"

    def _contradicted(self, plan: Plan, ticked: set[str], snapshot: Snapshot) -> set[str]:
        """The ticked subgoals of the current window that hold a value or a state the snapshot
        positively contradicts (correction 1): a dependent select reset an earlier choice."""
        window = self._ledger.window_ref
        found: set[str] = set()
        for subgoal in plan.subgoals:
            own = [s for s in plan.steps if s.subgoal == subgoal.id]
            here = bool(own) and all(self._step_refs.get(s.id) == window for s in own)
            leaves = _firm(subgoal.condition) if subgoal.id in ticked and here else []
            standing = [leaf for leaf in leaves if not _superseded(plan, subgoal, leaf, ticked)]
            if any(self._reads_otherwise(leaf, snapshot) for leaf in standing):
                found.add(subgoal.id)
        return found

    def _reads_otherwise(self, leaf: FieldValue | CheckState, snapshot: Snapshot) -> bool:
        """Exactly one element, readable, not secure, and reading another value or state. Gone,
        twinned, patternless, unreadable (a value read empty included: T1 reads an unreadable
        value as empty) or secure keeps the tick."""
        try:
            verdict = evaluate(leaf, snapshot, snapshot, live=self._live, pinned=self._pins)
        except (SteerError, ValueError):
            return False
        return not verdict.holds and verdict.evidence.get("reason") in _CONTRADICTIONS

    def _untick(self, ticked: set[str], reset: set[str]) -> None:
        """Withdraw the ticks a re-read contradicted: a token record per subgoal (W4-04)."""
        for subgoal_id in sorted(reset & ticked):
            ticked.discard(subgoal_id)
            record = {"kind": "untick", "task_id": self.task_id, "subgoal": subgoal_id}
            self._trace.write({**record, "detail": "reset"})

    def _refused_start(
        self, plan: Plan, index: int, ticked: set[str]
    ) -> tuple[Literal["stopped"], ErrorCode, str] | None:
        """Why the step at `index` must not start: an irreversible step acts only on reached
        preconditions, so a failed chat click never lets the reply land in whichever chat is
        open (#124); and the step budget."""
        if _unmet_before(plan, index, ticked):
            self._trace_tick(plan.steps[index].subgoal, "none", detail="precondition_unmet")
            return "stopped", ErrorCode.VERIFY_FAILED, "precondition_unmet"
        if _unscoped_after_present(plan, index):
            # raised to R2/R3 by a deny word, with nothing excluded from the present (#124)
            self._trace_tick(plan.steps[index].subgoal, "none", detail="content_unscoped")
            return "stopped", ErrorCode.GATE_BLOCKED, "content_unscoped"
        if self._steps_run >= self.max_steps:
            return "stopped", ErrorCode.BUDGET_EXCEEDED, "max_steps"
        return None

    def _after_open(
        self, plan: Plan, step: Step, results: list[StepResult], ticked: set[str]
    ) -> tuple[Literal["stopped", "paused", "blocked"], ErrorCode, str] | None:
        """A failed `open` stops: the next steps would act in the old window, maybe the
        person's own (P6); the last verified open of an open-only subgoal ticks it when its
        condition holds on that window (5.5 [v2.6])."""
        result = results[-1]
        stop = _stop_for(result)
        if stop is not None:
            return stop
        if not result.ok:
            detail = str(result.evidence.get("detail", result.gate))
            return "stopped", result.code or ErrorCode.VERIFY_TIMEOUT, detail
        subgoal = next(s for s in plan.subgoals if s.id == step.subgoal)
        own = [s for s in plan.steps if s.subgoal == subgoal.id]
        if not self._open_judges(subgoal, own, results):
            return None
        try:
            self._checked(self._identity_now)  # the window may have gone since (OS-04)
        except _StepFailureError as failure:
            self._trace_tick(subgoal.id, "none", detail=failure.detail)
            return None
        if len(own) == 1 and open_title_judges(subgoal.condition):
            # the code-owned title the open verified is the evidence (gate R1-R3, issue #47)
            site, text = step.params.get("site"), step.params.get("text")
            title = expected_title(step.target.name if step.target else "", site, text)
            by, holds = "open_title", open_condition_holds(subgoal.condition, title)
        else:
            by, holds = "open_window", self._holds_in_window(subgoal.condition)
        if holds:
            ticked.add(subgoal.id)
            self._trace_tick(subgoal.id, by)
        return None

    def _open_judges(self, subgoal: Subgoal, own: list[Step], results: list[StepResult]) -> bool:
        """Whether an open may judge its subgoal: only opens, in this plan and every earlier one
        (an open that ticked a subgoal with other steps skipped them, a false done: hunter
        OS-02/03); no value or box state (a type or check step's doing, FV-01/03, W2-4); every
        open of it verified, so only its last one judges (OS-06)."""
        made = field_values(subgoal.condition) or check_states(subgoal.condition)
        if self._verbs.get(subgoal.id, set()) - {"open"} or made:
            return False
        if not self._verbs_complete:
            self._trace_tick(subgoal.id, "none", detail="verbs_unknown")
            return False
        verified = {r.step_id for r in results if r.ok}
        return all(s.id in verified for s in own)

    def _trace_tick(
        self, subgoal_id: str, by: str, *, detail: str | None = None, alias: str | None = None
    ) -> None:
        """What ticked a subgoal, as tokens: a plan id (plan_io's id pattern), never a title,
        pattern or content (hunter OS-20/23/24); a value condition's alias role (#76)."""
        record: dict[str, Any] = {
            "kind": "tick",
            "task_id": self.task_id,
            "subgoal": subgoal_id,
            "by": by,
        }
        if detail is not None:
            record["detail"] = detail
        if alias in FIELD_ROLES:  # a role token from the verdict, never content
            record["alias"] = alias
        self._trace.write(record)

    def _holds_now(self, predicate: Predicate, snapshot: Snapshot) -> bool:
        """Presence conditions already true at task start; diff conditions never tick here, and
        neither does `present`: its click always runs, so the selection is made, not assumed
        (#124: a chat open at start may be another person's, hunter PS-01/10/19)."""
        if isinstance(predicate, _DIFF_PREDICATES) or presents(predicate):
            return False
        if isinstance(predicate, TitleMatches):
            return title_holds(predicate.pattern, snapshot.title)
        try:
            return evaluate(
                predicate,
                snapshot,
                snapshot,
                live=self._live,
                windows=self._windows() if self._windows else None,
                windows_before=list(self._windows()) if self._windows else None,
                pinned=self._pins,
            ).holds
        except (SteerError, ValueError):
            return False

    def _expect_of(self, step: Step) -> Predicate | None:
        """The plan's expect, a `present` bound to the plan's content landmarks (#124)."""
        if isinstance(step.expect, Present):
            return replace(step.expect, excluding=self._content_landmarks)
        return step.expect

    def _condition_verdict(
        self, condition: Predicate, before: Snapshot, after: Snapshot
    ) -> Verdict | None:
        """A `present` condition holds only through its own click's bound, verified proof: an
        unbound one would count the clicked row's text, and any other step proves nothing
        about the selection (#124, hunter PS-02/03/20)."""
        if not presents(condition):
            proven = self._url_proven(condition)
            return proven if proven is not None else self._subgoal_verdict(condition, before, after)
        if self._proof is None:
            return None
        proof, verdict = self._proof
        same = (
            isinstance(proof, Present)
            and isinstance(condition, Present)
            and replace(proof, target_id=None, excluding=()) == condition
        )
        return verdict if same else None

    def _url_proven(self, condition: Predicate) -> Verdict | None:
        """The address bar's proof of this step's submit (#148), for the step's own subgoal
        only: its exact expect, or a `title` naming the typed words under the title key (H3
        pypi1, #198: results pages titled without the words, [v2.51]), evidence
        `url_query_title`. Nothing else is proven by it."""
        if self._url_proof is None:
            return None
        expect, verdict, text = self._url_proof
        if expect == condition:
            return verdict
        if isinstance(condition, TitleMatches) and same_title(condition.pattern, text):
            return replace(verdict, evidence={**verdict.evidence, "reason": "url_query_title"})
        return None

    def _subgoal_holds(self, predicate: Predicate, before: Snapshot, after: Snapshot) -> bool:
        verdict = self._subgoal_verdict(predicate, before, after)
        return verdict is not None and verdict.holds

    def _subgoal_verdict(
        self, predicate: Predicate, before: Snapshot, after: Snapshot
    ) -> Verdict | None:
        """The condition's verdict, or None when it cannot be judged (an error never holds)."""
        try:
            return evaluate(
                predicate,
                before,
                after,
                live=self._live,
                windows=self._windows() if self._windows else None,
                windows_before=self._windows_at_step,
                pinned=self._pins,
            )
        except (SteerError, ValueError):
            return None


ACT_MISMATCH = "screen_changed_during_act"
"""A re-resolve inside the act loop (the retry after a refusal, the scroll) found another
element than the one asked about (#89): `focus_lost`, nothing more dispatched."""


def _note_reasons(evidence: dict[str, Any], gate_result: GateResult) -> None:
    """Why a step below R2 asked, as closed tokens, on every answer (#81 part A review)."""
    if "ask_reasons" in gate_result.evidence:
        evidence["ask_reasons"] = gate_result.evidence["ask_reasons"]


def _same_element(asked: Element, now: Element) -> bool:
    """The element asked about, still: the same UIA RuntimeId and role (#89). An element
    without a RuntimeId cannot be proven the same: unclear means a changed screen."""
    return bool(asked.runtime_id) and now.runtime_id == asked.runtime_id and now.role == asked.role


FRAME_HOST = "applicationframehost.exe"
WINDOW_TITLE_CHARS = 80
"""How much of a page's own title an ask shows: a page sets it, so code caps it."""


def _needs_front(step: Step) -> bool:
    """From the plan alone: a physical step or an R2/R3 step (a submit is always R2, its floor)
    needs its window in front; a semantic R0/R1 step does not (SF-13)."""
    path = step.path or executor_verb(step.verb)[0]
    return path == "physical" or effective_risk(step) in ("R2", "R3")


def _value_ready(plan: Plan, step: Step, result: StepResult, results: list[StepResult]) -> bool:
    """A `value` or `state` subgoal is judged only after its last step, with no input from the
    person during any of its steps (rulings FV-01, FV-20, W2-4); a typed `value` only after a
    step below R2, while a consent box's check is R2 by design, and so may a choose's value be
    (W4 v1 verdict item 4: only when that last step is a choose and every value is its)."""
    subgoal = next(s for s in plan.subgoals if s.id == step.subgoal)
    values = field_values(subgoal.condition)
    if not values and not check_states(subgoal.condition):
        return True
    own = [s.id for s in plan.steps if s.subgoal == subgoal.id]
    if own[-1] != step.id:
        return False
    chosen = step.verb == "choose" and all(v.folded for v in values)
    if values and not chosen and result.evidence.get("risk") in ("R2", "R3"):
        return False
    return not any(r.evidence.get("user_input_during_step") for r in results if r.step_id in own)


def _ledger_ref(window: str) -> str | None:
    """A step's `window` as the ledger's window ref: `start` is None."""
    return None if window == START_WINDOW else window


def _plan_refs(plan: Plan, link: str | None) -> dict[str, str | None]:
    """Each step's window from plan order, skipped steps included (SW-01): the link's
    start, changed by a step's `window` and, after it, by an `open`."""
    refs: dict[str, str | None] = {}
    current = link
    for step in plan.steps:
        if step.window is not None:
            current = _ledger_ref(step.window)
        refs[step.id] = current
        if step.verb == "open":
            current = f"opened:{step.id}"
    return refs


def _unmet_before(plan: Plan, index: int, ticked: set[str]) -> bool:
    """An R2/R3 step (by its plan tier and deny words) with an earlier subgoal unticked whose
    steps ran before it; the person-candidate ceiling is judged inside the step."""
    step = plan.steps[index]
    if effective_risk(step) not in ("R2", "R3"):
        return False
    return _earlier_unticked(plan, index, ticked)


def _earlier_unticked(plan: Plan, index: int, ticked: set[str]) -> bool:
    """An earlier subgoal, other than this step's own, whose steps ran and is not ticked."""
    step = plan.steps[index]
    earlier = {s.subgoal for s in plan.steps[:index]} - {step.subgoal}
    return bool(earlier - ticked)


def _unscoped_after_present(plan: Plan, index: int) -> bool:
    """An R2/R3 step (deny words count) in a plan with a `present` that names no landmark for
    its content: the plan boundary refuses the plan-tier case, this is the raised one."""
    step = plan.steps[index]
    if isinstance(step.expect, Present):
        return False  # its own present above R1 is the gate's `present_alone` block
    if effective_risk(step) not in ("R2", "R3") or names_content_landmark(step):
        return False
    held = [s.condition for s in plan.subgoals] + [s.expect for s in plan.steps if s.expect]
    return any(presents(p) for p in held)


def _future_subgoals(plan: Plan) -> set[str]:
    """Step-less subgoals listed after a subgoal that has steps: a state the plan has not
    reached yet, so never true at start (5.11 [v2.40]; live: `title "YouTube"` as "the video
    is playing" ticked on the results page and the task ended `done`)."""
    owned = {s.subgoal for s in plan.steps}
    future: set[str] = set()
    seen_steps = False
    for subgoal in plan.subgoals:
        if subgoal.id in owned:
            seen_steps = True
        elif seen_steps:
            future.add(subgoal.id)
    return future


def _leaves(predicate: Predicate) -> list[Predicate]:
    if isinstance(predicate, All | AnyOf):
        return [leaf for part in predicate.parts for leaf in _leaves(part)]
    return [predicate]


def _content_landmarks(plan: Plan) -> tuple[str, ...]:
    """Every landmark the plan's content checks name (a send's scope, an `appeared` within):
    where messages are, so never where a `present` may be proven (hunter PS-09)."""
    found: set[str] = set()
    predicates = [s.condition for s in plan.subgoals] + [
        s.expect for s in plan.steps if s.expect is not None
    ]
    for leaf in (leaf for p in predicates for leaf in _leaves(p)):
        within = leaf.within if isinstance(leaf, Appeared | Disappeared | CountDelta) else None
        if within is not None and within.landmark is not None:
            found.add(within.landmark)
    for step in plan.steps:
        scope = step.params.get("scope")
        if isinstance(scope, str) and scope.strip():
            found.add(normalise_name(scope).casefold())
    return tuple(sorted(found))


def _structural_part(predicate: Predicate) -> Predicate | None:
    """The diff part of a template the resume guard may evaluate on a handle-less snapshot."""

    if isinstance(predicate, _DIFF_PREDICATES):
        return predicate
    if isinstance(predicate, (All, AnyOf)):
        found = [p for p in (_structural_part(part) for part in predicate.parts) if p is not None]
        for candidate in found:
            if not isinstance(candidate, CountDelta):
                return candidate
        return found[0] if found else None
    return None


def _group_names(snapshot: Snapshot, elements: Sequence[Element]) -> list[str]:
    return [name for name in (group_name(snapshot, e) for e in elements) if name]


def _same_screen(before: Snapshot, after: Snapshot) -> bool:
    if before.title != after.title:
        return False
    marks_before = before.fingerprint.marks
    marks_after = after.fingerprint.marks
    if marks_before and marks_after:
        return similarity(marks_before, marks_after) >= SCREEN_GUARD_THRESHOLD
    return before.node_count == after.node_count


def _stop_for(
    result: StepResult,
) -> tuple[Literal["stopped", "paused", "blocked"], ErrorCode, str] | None:
    """Why the task ends after this result, or None: the person blocked or killed it, an ask
    is pending, nothing was dispatched (unreached, or a refusal after the gate ran), or the
    window is not there to act on any more."""
    detail = str(result.evidence.get("detail", result.gate))
    if result.gate == "block" or detail == "killed":
        return "blocked", ErrorCode.GATE_BLOCKED, detail
    if result.gate == "pending":
        return "paused", ErrorCode.GATE_PENDING_USER, detail
    if not result.ok and (result.action is None or result.code in STOP_CODES):
        return "stopped", result.code or ErrorCode.VERIFY_FAILED, detail
    if not result.ok and detail == NO_EFFECT:
        # the Enter changed nothing: the replan submits through the Button beside the field
        return "stopped", ErrorCode.VERIFY_FAILED, NO_EFFECT
    return None


def _state_stop(step: Step, result: StepResult) -> tuple[Literal["stopped"], ErrorCode, str] | None:
    """A widget verb dispatched at R2/R3 whose T1 missed ends the task: the person approved one
    tick, so it is never sent again in this task, by retry or by replan (W2 verdict, W2-5). At
    any tier, a miss whose target is gone or whose window was retitled means the verb navigated
    (W3-01, W4-01): it stops `navigated`. A verified state never stops, whatever the title does.
    A choose that left its box expanded stops: no further input in the window (W4-02, W4-08)."""
    if step.verb not in WIDGET_VERBS or result.ok or result.action is None:
        return None
    if result.evidence.get("reason") == "target_not_found" or result.evidence.get("retitled"):
        return "stopped", ErrorCode.VERIFY_FAILED, "navigated"
    if result.evidence.get("detail") == "left_expanded":
        return "stopped", result.code or ErrorCode.VERIFY_FAILED, "left_expanded"
    if step.verb == "choose" and _unchosen(result.action):
        return None  # a refusal sent no option: nothing to verify or to stop on (W4-06)
    if result.evidence.get("risk") not in ("R2", "R3"):
        return None
    return "stopped", result.code or ErrorCode.VERIFY_FAILED, "state_unverified"


MEASURED_PROVIDER = "chrome.exe"
"""The one provider whose state patterns are measured (probe `widget_patterns`, Chrome 156)."""


def _measured_provider(step: Step, snapshot: Snapshot) -> None:
    """A widget verb runs only in the measured provider (W3-04, W4-01): anything else is refused
    before the gate, nothing sent and no ask."""
    if step.verb in WIDGET_VERBS and snapshot.process.casefold() != MEASURED_PROVIDER:
        raise _StepFailureError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "unmeasured_provider")


LISTBOX_ITEM_ROLE = "ListItem"
LISTBOX_ROLE = "List"
_NOT_A_LISTBOX = frozenset({"Tree", "DataGrid"})


def _listbox_admission(step: Step, snapshot: Snapshot, element_id: int, live: object) -> None:
    """A `select` on a ListItem is admitted only as a listbox option (W3b, the principal's rule):
    the item exposes SelectionItem, its nearest List ancestor exposes Selection with
    CanSelectMultiple false, and no ComboBox (its options are `choose`'s, WV-11), Tree or
    DataGrid ancestor. Anything else is refused before any scroll, the gate or an ask, with
    nothing sent: `in_combobox`, `multi_select`, `not_a_listbox_item`, or `selection_unreadable`
    when the live reader cannot read the patterns (unclear means fail). Measured (probe
    `widget_patterns --section disclosure`, Chrome 156, n=3): an ARIA `role=listbox` and a
    `<select size>` both read Selection with CanSelectMultiple false, and their items expose
    SelectionItem. The ancestors are the snapshot's; the patterns are read live."""
    element = snapshot.element(element_id)
    if step.verb != "select" or element.role != LISTBOX_ITEM_ROLE:
        return
    container: Element | None = None
    parent_id = element.parent_id
    while parent_id is not None:
        ancestor = snapshot.element(parent_id)
        if ancestor.role == CHOOSE_ROLE:
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "in_combobox")
        if ancestor.role in _NOT_A_LISTBOX:
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "not_a_listbox_item")
        if container is None and ancestor.role == LISTBOX_ROLE:
            container = ancestor
        parent_id = ancestor.parent_id
    if container is None:
        raise _StepFailureError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "not_a_listbox_item")
    if not isinstance(live, ListboxReads):
        raise _StepFailureError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "selection_unreadable")
    try:
        selectable = live.has_selection_item(snapshot.handle(element_id))
        multiple = live.selection_container(snapshot.handle(container.id))
    except SteerError as exc:
        raise _StepFailureError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "selection_unreadable") from exc
    if not selectable or multiple is None:
        raise _StepFailureError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "not_a_listbox_item")
    if multiple:
        raise _StepFailureError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "multi_select")


_CONTRADICTIONS = frozenset({"value_differs", "state_differs"})
"""The T1 reasons that read an element positively otherwise (W4-04 correction 1)."""


def _superseded(
    plan: Plan, subgoal: Subgoal, leaf: FieldValue | CheckState, ticked: set[str]
) -> bool:
    """A later widget verb in plan order, whose subgoal ticked, set the same control (same role
    and exact name) again: its own verified state supersedes this leaf, so "open it, read it,
    close it" ends `done` (#210 review). A different control never supersedes: an exclusive
    accordion that collapses the first panel still withdraws that tick."""
    own = [index for index, step in enumerate(plan.steps) if step.subgoal == subgoal.id]
    last = max(own) if own else -1
    wanted = (leaf.role, normalise_name(leaf.name_pattern).casefold())
    return any(
        step.subgoal in ticked
        and step.verb in WIDGET_VERBS
        and step.target is not None
        and (step.target.role, normalise_name(step.target.name).casefold()) == wanted
        for step in plan.steps[last + 1 :]
    )


def _firm(condition: Predicate) -> list[FieldValue | CheckState]:
    """The values and states a condition needs whatever else holds: top level or under `all`,
    never under `any_of`, where another branch may still hold."""
    if isinstance(condition, FieldValue | CheckState):
        return [condition]
    if isinstance(condition, All):
        return [leaf for part in condition.parts for leaf in _firm(part)]
    return []


def _unchosen(action: ActionResult) -> bool:
    """A choose that sent no Select (W4-06): it closes as a refusal, with no T1. An option
    already chosen (nothing sent) is verified like any skip; a Select that went out is read by
    T1 even when the box was left expanded, so a page the choose navigated away stops
    `navigated` (W4-12; measured: the Collapse after a submitting Select either fails
    0x80040201 or reads Expanded to the cap)."""
    if action.skipped:
        return False
    return not any(s.label == "SelectionItemPattern.Select" for s in action.steps)


def _verify_deadline(expect: Predicate, step_deadline_s: float) -> float:
    """The navigation form waits for a page, not a message."""
    if _is_navigation(expect):
        return max(step_deadline_s, NAVIGATION_VERIFY_S)
    return step_deadline_s


def _is_navigation(predicate: Predicate) -> bool:
    """Only a bare replacing `appeared`: the model's mirror cannot combine predicates, and a
    file plan that wraps one in `all` / `any_of` keeps the step deadline."""
    return isinstance(predicate, Appeared) and predicate.replaces


def _wal_detail(exc: LedgerError) -> str:
    """`ledger_locked` when another runner took the task (5.4 [v1.3], #58), else the ledger
    could not write (`wal_unavailable`)."""
    return "ledger_locked" if exc.locked else "wal_unavailable"


def _document_names(snapshot: Snapshot) -> list[str]:
    return [element.name for element in snapshot.elements if element.role == "Document"]


def _top_document(snapshot: Snapshot) -> Element | None:
    """The one Document with no Document above it; none or several is unclear."""
    by_id = {e.id: e for e in snapshot.elements}
    tops = []
    for element in snapshot.elements:
        if element.role != "Document":
            continue
        parent = by_id.get(element.parent_id) if element.parent_id is not None else None
        while parent is not None and parent.role != "Document":
            parent = by_id.get(parent.parent_id) if parent.parent_id is not None else None
        if parent is None:
            tops.append(element)
    return tops[0] if len(tops) == 1 else None


def _same_top_document(before: Snapshot, after: Snapshot) -> bool:
    """The top Document kept its RuntimeId and its name; one without a RuntimeId cannot be
    proven the same. A same-URL reload gives the top Document a new RuntimeId under the old
    name (NE-13; measured Chrome 156, 3/3), so this fact catches a reload too."""
    was, now = _top_document(before), _top_document(after)
    if was is None or now is None or not was.runtime_id:
        return False
    return now.runtime_id == was.runtime_id and now.name == was.name


def _composer_target(step: Step) -> tuple[str, str] | None:
    """The field an Enter submit holds its text in: the composer its params name, else the
    step's own text-role target."""
    role, name = step.params.get("composer_role"), step.params.get("composer_name")
    if role is not None and name is not None:
        return role, name
    if step.target is not None and step.target.role in TEXT_ROLES:
        return step.target.role, step.target.name
    return None


def _shares_form(snapshot: Snapshot, button_id: int, composer_id: int) -> bool:
    """The Button and the composer have the same nearest form container (`FORM_ROLES`,
    nearest semantics: each walks up to its first such ancestor, and those must be one
    element); either without one is not beside the other (fails closed)."""
    button, composer = _nearest(snapshot, button_id), _nearest(snapshot, composer_id)
    return button is not None and button == composer


def _nearest(snapshot: Snapshot, element_id: int) -> int | None:
    """The id of the nearest ancestor whose role is a form container's, or None."""
    by_id = {e.id: e for e in snapshot.elements}
    parent_id = by_id[element_id].parent_id
    while parent_id is not None:
        parent = by_id[parent_id]
        if parent.role in FORM_ROLES:
            return parent.id
        parent_id = parent.parent_id
    return None


def _one_field(snapshot: Snapshot, role: str, name: str) -> int | None:
    """Exactly one element with that role and name, else None."""
    found = [e for e in snapshot.elements if e.role == role and _same_name(e, name)]
    return found[0].id if len(found) == 1 else None


def _new_outside(before: Snapshot, after: Snapshot, composer_id: int) -> bool:
    """An element of `after` whose RuntimeId is new since `before` (or missing), outside the
    composer's nearest container (its parent and everything under it)."""
    known = {e.runtime_id for e in before.elements if e.runtime_id}
    container = after.element(composer_id).parent_id
    exempt = {composer_id} if container is None else _under(after, container)
    return any(
        e.id not in exempt and (not e.runtime_id or e.runtime_id not in known)
        for e in after.elements
    )


def _under(snapshot: Snapshot, root_id: int) -> set[int]:
    """The ids of every element under `root_id`, the root included."""
    children: dict[int | None, list[int]] = {}
    for element in snapshot.elements:
        children.setdefault(element.parent_id, []).append(element.id)
    found: set[int] = set()
    pending = [root_id]
    while pending:
        current = pending.pop()
        if current not in found:
            found.add(current)
            pending.extend(children.get(current, []))
    return found


def _same_name(element: Element, name: str) -> bool:
    return normalise_name(element.name).casefold() == normalise_name(name).casefold()
