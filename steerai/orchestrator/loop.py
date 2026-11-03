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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.executor import Action
from steerai.action.physical import PhysicalAction
from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.contracts import (
    GateDecision,
    GateToken,
    Plan,
    Risk,
    Step,
    StepResult,
    Target,
    executor_verb,
)
from steerai.orchestrator.gate import Gate, GateResult, effective_risk
from steerai.orchestrator.ledger import LedgerError, WriteAheadRecord
from steerai.orchestrator.trace import NullTrace, Trace
from steerai.verify.predicates import (
    Appeared,
    CountDelta,
    Disappeared,
    Predicate,
    TitleMatches,
    WindowGone,
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
from steerai.world.fingerprint import similarity
from steerai.world.model import Snapshot, normalise_name

REUSE_FRESHNESS_S = 5.0
"""How old a decided after snapshot may be to serve as the next R0/R1 before. Unmeasured; well
inside the executors' 10 s admission cap."""
SCREEN_GUARD_THRESHOLD = 0.9
SCREEN_GUARD_STRIKES = 3
DEFAULT_MAX_STEPS = 20
_DIFF_PREDICATES = (Appeared, Disappeared, CountDelta, WindowGone)


class Observer(Protocol):
    def observe(self, hwnd: int, *, stable_window: int = ..., cap_s: float = ...) -> Snapshot: ...


class ExecutorLike(Protocol):
    def execute(self, action: Any, snapshot: Snapshot) -> ActionResult: ...


class TaskLedger(Protocol):
    def key_for(self, step: Step) -> str: ...
    def admit(self, key: str, decision: GateDecision | None = None) -> str: ...
    def outcome_of(self, key: str) -> str | None: ...
    def write_ahead(
        self, step: Step, before: Snapshot, decision: GateDecision | None
    ) -> WriteAheadRecord: ...
    def mark(self, step: Step, phase: Any) -> None: ...
    def record_outcome(self, step: Step, outcome: str) -> None: ...
    def read_ahead(self, step: Step) -> WriteAheadRecord | None: ...
    def before_of_record(self, step: Step, fresh: Snapshot) -> Snapshot: ...


@dataclass(frozen=True, slots=True)
class TaskResult:
    status: Literal["done", "stopped", "paused", "blocked"]
    results: list[StepResult]
    ticked: set[str]
    code: ErrorCode | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


class _StepFailureError(Exception):
    def __init__(self, code: ErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


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
    ) -> None:
        self.task_id = task_id
        self.hwnd = hwnd
        self.max_steps = max_steps
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

    # ---- one step ----------------------------------------------------------------------------

    def run_step(self, step: Step) -> StepResult:  # noqa: PLR0911 - one return per exit
        stage = _Stage(self._monotonic)
        risk = effective_risk(step)
        key = self._ledger.key_for(step)
        evidence: dict[str, Any] = {"risk": risk}
        gate_stage: GateToken = "unreached"
        windows_before = list(self._windows()) if self._windows else None
        self._begin_step(windows_before)
        try:
            before = stage.measure("observe", lambda: self._before(risk, evidence))
            element_id = stage.measure("decide", lambda: self._resolve(step, before))
            previous = self._admission(step, key, risk, before)
            if previous == "done":
                return self._done_already(step, stage, evidence)
            gate_result = stage.measure(
                "gate",
                lambda: self._gate.decide(
                    step,
                    action_key=key,
                    expect=step.expect,
                    target_pid=before.pid,
                    previous_attempt=previous,
                    target_id=element_id,
                    composer_id=self._composer(step, before),
                ),
            )
            gate_stage = gate_result.outcome
            if gate_result.outcome in ("block", "pending"):
                return self._gate_stopped(step, gate_result, stage, evidence)
            decision, expect = self._admitted(key, previous, gate_result)
            if gate_result.outcome == "confirm":
                # The approved thing must be the executed thing (OR-31): observe again.
                before = stage.measure("observe", lambda: self._observe_fresh(evidence))
                element_id = stage.measure("decide", lambda: self._resolve(step, before))
            before_of_record = before
            if risk in ("R2", "R3"):
                try:
                    before_of_record = self._ledger.before_of_record(step, before)
                    self._ledger.write_ahead(step, before_of_record, decision)
                except LedgerError as exc:
                    raise _StepFailureError(ErrorCode.GATE_BLOCKED, "wal_unavailable") from exc
            action, before, retries = stage.measure(
                "act", lambda: self._act(step, risk, before, element_id)
            )
            evidence["retries"] = retries
            evidence["delivery"] = action.delivery.value
            if self._scrolled is not None:
                evidence["scrolled"] = self._scrolled
            if action.code in (ErrorCode.GATE_BLOCKED, ErrorCode.GATE_PENDING_USER):
                self._close(step, risk, action.delivery.value)
                return self._finish(
                    step,
                    stage,
                    evidence,
                    gate=gate_result.outcome,
                    action=action,
                    verify_code=None,
                    label_kind=None,
                    code=action.code,
                )
            if action.delivery is Delivery.NOT_DELIVERED and not action.steps:
                self._close(step, risk, "not_delivered")
                return self._finish(
                    step,
                    stage,
                    evidence,
                    gate=gate_result.outcome,
                    action=action,
                    verify_code=None,
                    label_kind=None,
                    code=action.code,
                )
            verdict, after = stage.measure(
                "verify", lambda: self._verify(expect, before_of_record, windows_before)
            )
            label = label_of_record(
                verdict, predicate=expect, action_key=key, before=before_of_record, after=after
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
                    gate=gate_result.outcome,
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
                gate=gate_result.outcome,
                action=action,
                verify_code=verdict.code,
                label_kind=label.kind if label else None,
                ok=ok,
                code=None if ok else (verdict.code or action.code),
            )
        except _StepFailureError as failure:
            # before the gate ran the token is `unreached`; after, the gate's outcome
            evidence["detail"] = failure.detail
            return self._finish(
                step,
                stage,
                evidence,
                gate=gate_stage,
                action=None,
                verify_code=None,
                label_kind=None,
                code=failure.code,
            )

    # ---- stages ---------------------------------------------------------------------------------

    def _begin_step(self, windows_before: list[WindowLike] | None) -> None:
        self._windows_at_step = windows_before
        self._scrolled = None

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
            if same_window and snapshot.stable and self._monotonic() - stamp <= REUSE_FRESHNESS_S:
                evidence["observe_reused"] = True
                return snapshot
        return self._observe_fresh(evidence)

    def _resolve(self, step: Step, snapshot: Snapshot) -> int:
        target = step.target
        if target is None:  # enforced by Step.__post_init__; kept for the type checker
            raise _StepFailureError(ErrorCode.ELEMENT_NOT_FOUND, "no_target")
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
        try:
            composer_id = self._composer(step, record.before)
        except _StepFailureError:
            return record.phase
        expect = self._gate.predicate_for(step, composer_id=composer_id)
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
        executor = self._physical if path == "physical" else self._semantic
        retries = 0
        scrolled = False
        while True:
            if risk in ("R2", "R3"):
                try:
                    self._ledger.mark(step, "acting")
                except LedgerError as exc:
                    raise _StepFailureError(ErrorCode.GATE_BLOCKED, "wal_unavailable") from exc
            action = self._build(path, verb, element_id, step.text)
            result = executor.execute(action, before)
            if result.code in (ErrorCode.GATE_BLOCKED, ErrorCode.GATE_PENDING_USER):
                return result, before, retries
            if result.delivery is not Delivery.NOT_DELIVERED:
                return result, before, retries
            detail = result.evidence.get("detail")
            if detail in ("offscreen", "clipped") and not scrolled:
                scrolled = True
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

    @staticmethod
    def _build(path: str, verb: str, element_id: int, text: str | None) -> Any:
        if path == "physical":
            return PhysicalAction(verb=verb, element_id=element_id, text=text)
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
            deadline_s=self._verify_deadline_s,
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

    def _done_already(self, step: Step, stage: _Stage, evidence: dict[str, Any]) -> StepResult:
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

    def run_task(self, plan: Plan) -> TaskResult:
        self._trace.write(
            {
                "kind": "task",
                "task_id": self.task_id,
                "steps": len(plan.steps),
                "subgoals": len(plan.subgoals),
            }
        )
        start = self._observer.observe(self.hwnd)
        ticked = {s.id for s in plan.subgoals if self._holds_now(s.condition, start)}
        results: list[StepResult] = []
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

    def _run_steps(
        self, plan: Plan, ticked: set[str], results: list[StepResult]
    ) -> tuple[Literal["stopped", "paused", "blocked"], ErrorCode, str] | None:
        strikes = 0
        for step in plan.steps:
            if step.subgoal in ticked:
                continue
            if len(results) >= self.max_steps:
                return "stopped", ErrorCode.BUDGET_EXCEEDED, "max_steps"
            result = self.run_step(step)
            results.append(result)
            detail = str(result.evidence.get("detail", result.gate))
            if result.gate == "block":
                return "blocked", ErrorCode.GATE_BLOCKED, detail
            if result.gate == "pending":
                return "paused", ErrorCode.GATE_PENDING_USER, "unanswered"
            if result.gate == "unreached":
                return "stopped", result.code or ErrorCode.VERIFY_FAILED, detail
            if result.gate == "done_already":
                # the earlier verified attempt (or the persisted before) proved the effect
                ticked.add(step.subgoal)
                strikes = 0
                continue
            if result.ok and self._last_pair is not None:
                before, after = self._last_pair
                subgoal = next(s for s in plan.subgoals if s.id == step.subgoal)
                if self._subgoal_holds(subgoal.condition, before, after):
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


def _structural_part(predicate: Predicate) -> Predicate | None:
    """The diff part of a template the resume guard may evaluate on a handle-less snapshot."""
    from steerai.verify.predicates import All, AnyOf  # noqa: PLC0415 - avoids an import cycle

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
