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
from steerai.action.executor import Action
from steerai.action.launch import APPS, expected_title, title_matches
from steerai.action.launcher import OpenAction
from steerai.action.physical import TEXT_ROLES, PhysicalAction
from steerai.action.sanitize import normalise_newlines
from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.contracts import (
    RISKS,
    START_WINDOW,
    GateDecision,
    GateToken,
    Plan,
    Risk,
    Step,
    StepResult,
    Target,
    executor_verb,
)
from steerai.orchestrator.gate import Gate, GateResult, effective_risk, open_tier, params_for
from steerai.orchestrator.identity import (
    Binding,
    bind,
    other_role_matches,
    pick,
    rebind,
    too_many,
)
from steerai.orchestrator.ledger import LedgerError, WriteAheadRecord
from steerai.orchestrator.trace import NullTrace, Trace
from steerai.verify.predicates import (
    All,
    AnyOf,
    Appeared,
    CountDelta,
    Disappeared,
    Predicate,
    TitleMatches,
    WindowExists,
    WindowGone,
    field_values,
)
from steerai.verify.t1 import (
    NO_LABEL_REASONS,
    LiveReads,
    WindowLike,
    evaluate,
    label_of_record,
    title_holds,
    verify,
)
from steerai.world.elements import Element
from steerai.world.fingerprint import similarity
from steerai.world.model import Snapshot, normalise_name
from steerai.world.people import MAX_CANDIDATES, comparable, lookalike_only, name_part

REUSE_FRESHNESS_S = 5.0
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


class Session:
    def __init__(  # noqa: PLR0913 - every collaborator is injected so the loop runs pure in tests
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
    ) -> None:
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
        self._foreground = foreground
        self._goal = ""
        self._monitor = monitor
        self._counts_at_step: dict[str, int] = {}
        self._secure_step = False
        self.hwnd = hwnd
        self.max_steps = max_steps
        self._steps_run = 0
        """Steps run by this session over all its plans: a replanned task is one budget."""
        self._observer = observer
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
        self._windows_at_step: list[WindowLike] | None = None
        self._scrolled: str | None = None
        self._act_evidence: dict[str, Any] = {}
        self._sent_in_act = False
        self._binding: Binding | None = None
        self._candidates: tuple[str, list[Element]] | None = None

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
            self._boundary(evidence)
            self._select(step, evidence)  # the window first: it is part of the key
            key = self._ledger.key_for(step)
            self._act_evidence = evidence
            before = stage.measure("observe", lambda: self._before(risk, evidence))
            # admission first, at the candidates' tier ceiling for a person target: a done
            # or open record under this key is never bypassed by the row the person picks
            risk = self._ceiling(step, key, before, risk)
            if risk in ("R2", "R3") and evidence.get("observe_reused"):
                # a reused R0/R1 snapshot is not a before for an R2/R3 step: observe fresh
                before = stage.measure("observe", lambda: self._observe_fresh(evidence))
                risk = self._ceiling(step, key, before, effective_risk(step))
            previous = self._admission(step, key, risk, before)
            if previous == "done":
                return self._done_already(step, key, stage, evidence)
            before, element_id = self._resolve_settling(step, before, stage, evidence)
            self._secure_step = before.element(element_id).secure
            risk = self._settled_tier(step, risk)
            evidence["risk"] = risk
            self._in_front(risk)
            gate_result = stage.measure(
                "gate", lambda: self._decide(step, key, before, previous, element_id, risk=risk)
            )
            gate_stage = gate_result.outcome
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
                before, element_id = stage.measure(
                    "observe", lambda: self._reobserve(step, before, evidence, mismatch=token)
                )
                if self._binding is not None:
                    # the bound row's text may have moved on: the postcondition follows it,
                    # through the gate's own strength rung (never a weaker check)
                    expect = self._rebuilt(step, risk, before, element_id)
                if waited:
                    evidence["restarted"] = True
            before_of_record, recorded = self._record(step, risk, before, decision)
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
            gate_result = stage.measure(
                "gate", lambda: self._decide(step, key, before, previous, None, risk=risk)
            )
            gate_stage = gate_result.outcome
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
                raise _StepFailureError(ErrorCode.GATE_BLOCKED, "wal_unavailable") from exc
        windows_before = list(self._windows())  # the last thing before the launch (P4)
        launcher = self._launcher
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
            return self._finish(step, stage, evidence, gate=gate, action=action, code=action.code)
        holds = stage.measure("verify", lambda: self._title_arrives(hwnd, title))
        identity = self._identify(hwnd) if holds else None
        images = APPS[step.target.name].process_names if step.target.name in APPS else frozenset()
        if identity is not None and str(identity.get("image", "")).lower() not in images:
            identity = None  # the window is not the app's after all
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
            self._rebind(self._record_of(ref), ref)
            self._switched = True  # a window the task opened, as on the first run (SW-15)
        elif continuation and self._ledger.start_window() is not None:
            self._rebind(self._record_of(None), None)

    def _rebind(self, record: Mapping[str, Any] | None, ref: str | None) -> None:
        hwnd = self._checked(record)
        if hwnd != self.hwnd:
            self._adopt(hwnd, ref)
        else:
            self._ledger.set_window_ref(ref)  # the same window: what was observed holds
        self._identity_now = record

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
        if current.get("minimized") is not False:
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "minimized")
        return hwnd

    def _select(self, step: Step, evidence: dict[str, Any]) -> None:
        """The step's window from plan order, re-bound by identity; naming the current
        window is accepted and still checked (gate Q2)."""
        if step.id in self._step_refs:
            ref = self._step_refs[step.id]
        elif step.window is not None:
            ref = _ledger_ref(step.window)
        else:
            return
        if step.window is None and ref == self._ledger.window_ref:
            return
        record = self._record_of(ref)
        if (
            ref is None
            and record is not None
            and str(record.get("image", "")).lower() == FRAME_HOST
        ):
            # every UWP frame shares the host's pid and image: `window: start` waits for a
            # measurement (SW-11); a continuation there still runs (review item 1)
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "frame_host")
        self._rebind(record, ref)
        self._switched = True
        evidence["window"] = "selected"

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

    def _in_front(self, risk: Risk) -> None:
        """An R2/R3 step in a window the task selected or opened is in front when its ask
        opens: the person sees where a send lands; there is no selection tap (SW-15, gate
        item 2). The start window's own steps keep the measured send path (review item 3)."""
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
            snapshot = self._observer.observe(self.hwnd)
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
        if renamed_at_risk:
            # a page renamed in place keeps its RuntimeId: no new element, so never the
            # effect of an R2/R3 step (5.10 [v1.2]); unverified, and no label (review item 2)
            reason = {**verdict.evidence, "reason": "renamed_at_risk"}
            verdict = replace(verdict, holds=False, code=ErrorCode.VERIFY_TIMEOUT, evidence=reason)
        label = (
            None
            if renamed_at_risk
            else label_of_record(
                verdict, predicate=expect, action_key=key, before=before_of_record, after=after
            )
        )
        self._remember(before_of_record, after)
        outcome = self._outcome(verdict.holds, action, verdict.evidence.get("reason"))
        try:
            self._close(step, risk, outcome)
        except LedgerError:
            # the action ran; the attempt stays open in the ledger (unknown next time)
            evidence["detail"] = "wal_unavailable"
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
        ok = verdict.holds and action.delivery is not Delivery.NOT_DELIVERED
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
        self, step: Step, risk: Risk, before: Snapshot, decision: GateDecision
    ) -> tuple[Snapshot, bool]:
        """The write-ahead record before any R2/R3 dispatch; (before of record, written)."""
        if risk not in ("R2", "R3"):
            return before, False
        try:
            before_of_record = self._ledger.before_of_record(step, before)
            chosen = self._binding.to_record() if self._binding is not None else None
            self._ledger.write_ahead(step, before_of_record, decision, chosen=chosen)
        except LedgerError as exc:
            raise _StepFailureError(ErrorCode.GATE_BLOCKED, "wal_unavailable") from exc
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
            self._close(step, risk, "not_delivered")
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
        checked = self._gate.postcondition(step, step.expect, risk, params)
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
        if target is None or target.match != "person":
            return risk
        found = pick(before, target)
        self._candidates = (before.id, found)
        tiers: list[Risk] = [risk]
        if len(found) <= MAX_CANDIDATES:
            tiers += [effective_risk(step, comparable(name_part(c.name))) for c in found]
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

    def _settled_tier(self, step: Step, ceiling: Risk) -> Risk:
        """The bound row's own tier, never below the plan's (PT-31)."""
        if self._binding is None:
            return ceiling
        return effective_risk(step, self._binding.name_part)

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
        asking = self._monitor is not None and risk in ("R2", "R3")
        if asking and self._monitor is not None:
            self._monitor.mark_ask_open()
        try:
            return self._gate.decide(
                step,
                action_key=key,
                expect=step.expect,
                target_pid=before.pid,
                previous_attempt=previous,
                target_id=element_id,
                composer_id=self._composer(step, before),
                bound=self._binding,
                window=self.window_label(),
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

    def _after_ask(
        self, step: Step, before: Snapshot, stage: _Stage, evidence: dict[str, Any]
    ) -> tuple[Snapshot, int]:
        """The approved thing must be the executed thing (OR-31): observe again, on the screen
        the ask was made on (RN-13), and resolve the target again."""
        pair: tuple[Snapshot, int] = stage.measure(
            "observe", lambda: self._reobserve(step, before, evidence)
        )
        return pair

    def _reobserve(
        self,
        step: Step,
        before: Snapshot,
        evidence: dict[str, Any],
        *,
        mismatch: str = "screen_changed_during_ask",
    ) -> tuple[Snapshot, int]:
        fresh = self._observe_fresh(evidence)
        if fresh.title != before.title:
            raise _StepFailureError(ErrorCode.FOCUS_LOST, mismatch)
        # no settle here (OR-31): a target gone while the ask was open is a changed screen
        element_id = self._resolve(step, fresh)
        self._secure_step = fresh.element(element_id).secure
        return fresh, element_id

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
            except LedgerError:
                evidence["ledger"] = "wal_unavailable"  # the attempt stays open (D5)
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
        self._scrolled = None
        self._secure_step = False
        self._sent_in_act = False
        self._binding = None
        self._candidates = None
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
        snapshot = self._observer.observe(self.hwnd)
        if not snapshot.stable:
            snapshot = self._observer.observe(self.hwnd)
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
                return before, stage.measure("decide", partial(self._resolve, step, before))
            except _StepFailureError as failure:
                if person or failure.detail != "no_match" or settled >= SETTLE_RETRIES:
                    raise
            settled += 1
            evidence["settled"] = settled
            stage.measure("settle", partial(self._sleep, SETTLE_S))
            before = stage.measure("observe", lambda: self._observe_fresh(evidence))

    def _resolve(self, step: Step, snapshot: Snapshot) -> int:
        target = step.target
        if target is None:  # enforced by Step.__post_init__; kept for the type checker
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "no_target")
        if target.match == "person":
            return self._resolve_person(step, target, snapshot)
        if target.element_id is not None:
            try:
                snapshot.element(target.element_id)
            except SteerError as exc:
                raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "no_match") from exc
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
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "ambiguous_target")
        return matches[0].id

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

    def _composer(self, step: Step, snapshot: Snapshot) -> int | None:
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
        return self._resolve(probe, snapshot)

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
            composer_id = self._composer(step, record.before)
        except _StepFailureError:
            return record.phase
        expect = self._gate.predicate_for(step, composer_id=composer_id, bound=self._binding)
        if expect is not None and self._effect_present(expect, record.before, before):
            self._ledger.record_outcome(step, "done_already")
            return "done"
        return record.phase

    def _effect_present(self, predicate: Predicate, persisted: Snapshot, now: Snapshot) -> bool:
        if persisted.hwnd != now.hwnd or (persisted.pid and now.pid and persisted.pid != now.pid):
            return False
        structural = _structural_part(predicate)
        if structural is None:
            return False
        try:
            verdict = evaluate(structural, persisted, now)
        except (SteerError, ValueError):
            return False
        return verdict.holds and verdict.level != 3

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
        while True:
            self._still_the_window()  # a gone window never leaves an acting record
            if risk in ("R2", "R3"):
                try:
                    self._ledger.mark(step, "acting")
                except LedgerError as exc:
                    raise _StepFailureError(ErrorCode.GATE_BLOCKED, "wal_unavailable") from exc
            if self._boundary(self._act_evidence):
                # a wait here ended after the observe too: same screen, fresh target (TK-03)
                before, element_id = self._reobserve(
                    step, before, self._act_evidence, mismatch="screen_changed_during_wait"
                )
                self._act_evidence["restarted"] = True
            self._still_the_window()
            if self._monitor is not None:
                # input during the step is what lands after this point (D7)
                self._counts_at_step = self._monitor.counts()
            action = self._build(path, verb, element_id, step.text, chord)
            result, before, element_id = self._dispatch(step, executor, action, before)
            if result.delivery is not Delivery.NOT_DELIVERED or result.steps:
                self._sent_in_act = True  # something may have reached the app
            if result.code in (ErrorCode.GATE_BLOCKED, ErrorCode.GATE_PENDING_USER):
                return result, before, retries
            if result.delivery is not Delivery.NOT_DELIVERED:
                return result, before, retries
            detail = result.evidence.get("detail")
            if detail in ("offscreen", "clipped") and not scrolled:
                scrolled = True
                self._boundary(self._act_evidence)  # the scroll is a dispatch too
                scroll_result = self._semantic.execute(
                    Action(verb="scroll_into_view", element_id=element_id), before
                )
                self._scrolled = scroll_result.delivery.value
                before = self._observer.observe(self.hwnd, stable_window=1)
                element_id = self._resolve(step, before)
                continue
            if result.code is ErrorCode.ELEMENT_NOT_FOUND and retries == 0:
                retries += 1
                before = self._observer.observe(self.hwnd, stable_window=1)
                element_id = self._resolve(step, before)
                continue
            return result, before, retries

    def _dispatch(
        self, step: Step, executor: ExecutorLike, action: Any, before: Snapshot
    ) -> tuple[ActionResult, Snapshot, int]:
        """One execution. Enter on a composer first proves the composer shows the approved text
        (OR-31). A focus click refused for a transparent overlay under the pointer
        (`hit_test_miss`, nothing sent) falls back to UIA SetFocus on a fresh look if the
        boundary waited (TK-03), verified like any focus step (5.9 [v1.7])."""
        element_id: int = action.element_id
        if getattr(action, "chord", None) == "enter":
            self._check_composer(step, before, element_id)
        result = executor.execute(action, before)
        fallback = (
            step.verb == "focus"
            and result.delivery is Delivery.NOT_DELIVERED
            and result.evidence.get("detail") == "hit_test_miss"
        )
        if not fallback:
            return result, before, element_id
        if self._boundary(self._act_evidence):
            before, element_id = self._reobserve(
                step, before, self._act_evidence, mismatch="screen_changed_during_wait"
            )
            self._act_evidence["restarted"] = True
            if self._monitor is not None:
                # the wait's input is not the step's (D7), as in the act loop
                self._counts_at_step = self._monitor.counts()
        result = self._semantic.execute(Action(verb="focus", element_id=element_id), before)
        self._act_evidence["focus_fallback"] = "set_focus"
        return result, before, element_id

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
        if value is None or normalise_newlines(value) != normalise_newlines(text):
            raise _StepFailureError(ErrorCode.GATE_BLOCKED, "composer_differs")

    @staticmethod
    def _build(
        path: str, verb: str, element_id: int, text: str | None, chord: str | None = None
    ) -> Any:
        if path == "physical":
            return PhysicalAction(verb=verb, element_id=element_id, text=text, chord=chord)
        return Action(verb=verb, element_id=element_id)

    def _verify(
        self, expect: Predicate, before: Snapshot, windows_before: Sequence[WindowLike] | None
    ) -> tuple[Any, Snapshot]:
        decided: list[Snapshot] = [self._observer.observe(self.hwnd, stable_window=1)]

        def observe(**kwargs: Any) -> Snapshot:
            decided.append(self._observer.observe(self.hwnd, **kwargs))
            return decided[-1]

        verdict = verify(
            expect,
            before,
            decided[0],
            observe,
            live=self._live,
            windows=self._windows() if self._windows else None,
            windows_before=windows_before,
            deadline_s=_verify_deadline(expect, self._verify_deadline_s),
            monotonic=self._monotonic,
            sleep=self._sleep,
        )
        after = next((s for s in decided if s.id == verdict.after_id), decided[-1])
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

    def run_task(self, plan: Plan, *, continuation: bool = False) -> TaskResult:
        """Run one plan. A continuation (plan n>1 of a replanned task) ticks no subgoal at
        start: its conditions were written from the screen it starts on (hunter RP-01)."""
        self._goal = plan.goal
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
        results: list[StepResult] = []
        try:
            self._rebind_ref(continuation=continuation)
        except _StepFailureError as failure:
            return self._ended_at_start(failure.code, failure.detail)
        try:
            start = self._observer.observe(self.hwnd)
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
        ticked = {
            s.id
            for s in plan.subgoals
            if not continuation
            and s.id not in opening
            and s.id not in elsewhere
            and not field_values(s.condition)  # a value is a step's doing (FV-01/03)
            and self._holds_now(s.condition, start)
        }
        outcome = self._run_steps(plan, ticked, results)
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
        for step in plan.steps:
            if step.subgoal in ticked:
                continue
            if self._steps_run >= self.max_steps:
                return "stopped", ErrorCode.BUDGET_EXCEEDED, "max_steps"
            self._steps_run += 1
            result = self.run_step(step)
            results.append(result)
            stop = (
                self._after_open(plan, step, result, ticked)
                if step.verb == "open"
                else _stop_for(result)
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
                strikes = 0
                continue
            if result.ok and self._last_pair is not None:
                before, after = self._last_pair
                subgoal = next(s for s in plan.subgoals if s.id == step.subgoal)
                if _value_ready(plan, step, result, results) and self._subgoal_holds(
                    subgoal.condition, before, after
                ):
                    ticked.add(subgoal.id)
                    strikes = 0
                    continue
            if result.ok:
                strikes = 0
            elif self._last_pair is None or _same_screen(*self._last_pair):
                strikes += 1
            if strikes >= SCREEN_GUARD_STRIKES:
                return "stopped", ErrorCode.BUDGET_EXCEEDED, "loop"
        return None

    def _after_open(
        self, plan: Plan, step: Step, result: StepResult, ticked: set[str]
    ) -> tuple[Literal["stopped", "paused", "blocked"], ErrorCode, str] | None:
        """A failed `open` stops: the next steps would act in the old window, maybe the
        person's own (P6); a verified one ticks its subgoal when it holds in the new window."""
        stop = _stop_for(result)
        if stop is not None:
            return stop
        if not result.ok:
            detail = str(result.evidence.get("detail", result.gate))
            return "stopped", result.code or ErrorCode.VERIFY_TIMEOUT, detail
        subgoal = next(s for s in plan.subgoals if s.id == step.subgoal)
        # a value is the type step's doing, never the open's (field-value FV-01/03)
        if not field_values(subgoal.condition) and self._holds_in_window(subgoal.condition):
            ticked.add(subgoal.id)
        return None

    def _holds_now(self, predicate: Predicate, snapshot: Snapshot) -> bool:
        """Presence conditions already true at task start; diff conditions never tick here."""
        if isinstance(predicate, _DIFF_PREDICATES):
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
            ).holds
        except (SteerError, ValueError):
            return False

    def _subgoal_holds(self, predicate: Predicate, before: Snapshot, after: Snapshot) -> bool:
        try:
            return evaluate(
                predicate,
                before,
                after,
                live=self._live,
                windows=self._windows() if self._windows else None,
                windows_before=self._windows_at_step,
            ).holds
        except (SteerError, ValueError):
            return False


FRAME_HOST = "applicationframehost.exe"
WINDOW_TITLE_CHARS = 80
"""How much of a page's own title an ask shows: a page sets it, so code caps it."""


def _value_ready(plan: Plan, step: Step, result: StepResult, results: list[StepResult]) -> bool:
    """A `value` subgoal is judged only after its last step, which ran below R2, with no
    input from the person during any of its steps (rulings FV-01, FV-20)."""
    subgoal = next(s for s in plan.subgoals if s.id == step.subgoal)
    if not field_values(subgoal.condition):
        return True
    own = [s.id for s in plan.steps if s.subgoal == subgoal.id]
    if own[-1] != step.id or result.evidence.get("risk") in ("R2", "R3"):
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
    return None


def _verify_deadline(expect: Predicate, step_deadline_s: float) -> float:
    """The navigation form waits for a page, not a message."""
    if _is_navigation(expect):
        return max(step_deadline_s, NAVIGATION_VERIFY_S)
    return step_deadline_s


def _is_navigation(predicate: Predicate) -> bool:
    """Only a bare replacing `appeared`: the model's mirror cannot combine predicates, and a
    file plan that wraps one in `all` / `any_of` keeps the step deadline."""
    return isinstance(predicate, Appeared) and predicate.replaces
