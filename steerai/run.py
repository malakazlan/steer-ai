"""The task runner (design 5.1 [v1.3]): a plan file, a window, a console and an exit code.

    python -m steerai.run --plan plan.json --window "WhatsApp" [--task-id ID] [--max-risk R1]

Order of work, so a refused plan has no side effect (RN-46): read and validate the plan, check
the risk limit over the whole plan, open and bind the ledger, resolve the window, bring it to
the foreground, build the session, run. stdout carries exactly one JSON line on every exit
(RN-40); asks, candidates and warnings go to stderr. Exit codes: 0 done, 2 paused, 3 blocked,
4 stopped, 1 usage or environment error, 70 an unexpected error (RN-27/28).
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sys
import time
import traceback
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

from steerai.errors import ErrorCode, SteerError
from steerai.orchestrator.console import ConsoleConfirmer, escape_text
from steerai.orchestrator.contracts import RISKS, Plan, Risk, Step
from steerai.orchestrator.gate import Confirmer, effective_risk
from steerai.orchestrator.ledger import LedgerError
from steerai.orchestrator.loop import DEFAULT_MAX_STEPS, TaskResult, wait_for_idle
from steerai.orchestrator.plan_io import PlanError, encode_predicate, plan_from_bytes
from steerai.orchestrator.trace import JsonLinesTrace, NullTrace, Trace
from steerai.planner.contracts import PlannerError, PlanOutcome, PlanRequest
from steerai.probes.windows import WindowInfo
from steerai.session.monitor import DEFAULT_KILL_KEY, parse_kill_key
from steerai.world.model import Snapshot, normalise_name

EXIT_DONE = 0
EXIT_USAGE = 1
EXIT_PAUSED = 2
EXIT_BLOCKED = 3
EXIT_STOPPED = 4
EXIT_ERROR = 70
_STATUS_EXIT = {
    "done": EXIT_DONE,
    "paused": EXIT_PAUSED,
    "blocked": EXIT_BLOCKED,
    "stopped": EXIT_STOPPED,
}
DEFAULT_WARMUP_S = 10.0
"""First-contact settle cap (RN-18). Unmeasured as a default; Chrome's cold tree needed up to
25 s on CI in `tests/orchestrator/test_live.py`, ~0.5 s locally (probe 2026-09-27)."""
_LEDGER_TOKENS = frozenset({"ledger_locked", "plan_changed", "window_changed"})
_RISK_ORDER = {risk: index for index, risk in enumerate(RISKS)}


class UsageError(ValueError):
    """A command line or plan the runner cannot act on; exit 1 and nothing touched."""


class RunnerError(SteerError):
    def __init__(self, code: ErrorCode, detail: str, candidates: Sequence[WindowInfo] = ()) -> None:
        super().__init__(code, detail)
        self.detail = detail
        self.candidates = tuple(candidates)


@dataclass(frozen=True, slots=True)
class Options:
    plan: Path
    window: str | None
    hwnd: int | None
    task_id: str
    ledger_dir: Path
    trace: Path | None
    max_risk: Risk
    max_steps: int
    verify_deadline_s: float
    warmup_s: float
    allow_plan_change: bool
    allow_window_change: bool
    kill_key: str = DEFAULT_KILL_KEY
    goal: str | None = None
    """Plan from this goal first (the planner writes `plan`), instead of reading `plan`."""

    def replace(self, **changes: Any) -> Options:
        return dataclasses.replace(self, **changes)


class LedgerLike(Protocol):
    def bind_plan(self, digest: str, *, allow_change: bool = False) -> None: ...
    def bind_window(self, *, pid: int, title: str, allow_change: bool = False) -> None: ...
    def close(self) -> None: ...


class SessionLike(Protocol):
    def run_task(self, plan: Plan) -> TaskResult: ...


@dataclass(frozen=True, slots=True)
class Bound:
    """Everything the session builder needs once the window and the ledger are bound."""

    hwnd: int
    pid: int
    title: str
    task_id: str
    options: Options
    confirmer: Confirmer
    ledger: Any
    """The task ledger: `LedgerLike` for the runner, the loop's `TaskLedger` for the session."""
    trace: Trace
    monitor: Any = None
    """The takeover monitor (`steerai.session.monitor.HookMonitor`), or None in pure tests."""


@dataclass(frozen=True, slots=True)
class Deps:
    """The runner's collaborators, injected so the assembly runs pure in tests."""

    list_windows: Callable[[], Sequence[WindowInfo]]
    exclusions: Callable[[], tuple[set[int], set[int]]]
    """(hwnds, pids) that can never be the target: this console and its process tree (RN-15)."""
    acquire_foreground: Callable[[int], str]
    open_ledger: Callable[[Path, str], LedgerLike]
    build_session: Callable[[Bound], SessionLike]
    start_monitor: Callable[[Options], Any] | None = None
    screen: Callable[[int], tuple[str, Sequence[str]]] | None = None
    """(title, 6.2 lines) of a window after a warm-up observe; None only in pure tests."""
    planner: Callable[[PlanRequest], PlanOutcome] | None = None
    """The LLM planner (5.3); None only in pure tests or a `--plan` run."""
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    """Starts the takeover monitor (hooks and the kill hot key) for these options; None only in
    pure tests. The real runner always monitors: 5.1 has no tier exception."""

    def replace(self, **changes: Any) -> Deps:
        return dataclasses.replace(self, **changes)


# ---- pure parts --------------------------------------------------------------------------


def over_limit(plan: Plan, max_risk: Risk) -> Step | None:
    """The first step whose effective risk (floors and deny patterns included) is over the
    limit, checked over the whole plan before anything runs (RN-02)."""
    for step in plan.steps:
        if _RISK_ORDER[effective_risk(step)] > _RISK_ORDER[max_risk]:
            return step
    return None


def resolve_window(
    windows: Sequence[WindowInfo],
    needle: str,
    *,
    excluded_hwnds: set[int],
    excluded_pids: set[int],
) -> WindowInfo:
    """Exactly one visible window whose title contains the text (normalised, case-folded),
    outside the runner's own process tree; never a minimized one (RN-15/21/34/35/53)."""
    wanted = normalise_name(needle).casefold()
    if not wanted:
        raise UsageError("--window needs a non-empty title substring")
    visible = [w for w in windows if w.hwnd not in excluded_hwnds and w.pid not in excluded_pids]
    matches = [w for w in visible if wanted in normalise_name(w.title).casefold()]
    if not matches:
        raise RunnerError(ErrorCode.ELEMENT_NOT_FOUND, "no_match", visible)
    if len(matches) > 1:
        raise RunnerError(ErrorCode.ELEMENT_NOT_FOUND, "ambiguous_target", matches)
    if matches[0].minimized:
        raise RunnerError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "minimized", matches)
    return matches[0]


def warm_up(  # noqa: PLR0913 - keyword-only tuning knobs plus the injected clock and kill
    observe: Callable[[], Snapshot],
    *,
    cap_s: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = 0.25,
    kill: Callable[[], bool] | None = None,
) -> Snapshot:
    """First contact (5.1 [v0.6], RN-18): observe until three consecutive snapshots agree on
    the node count, or the cap passes; the last snapshot is returned either way. The kill
    switch is honoured between observes (TK-22)."""
    start = monotonic()
    counts: list[int] = []
    while True:
        if kill is not None and kill():
            raise SteerError(ErrorCode.GATE_BLOCKED, "killed: the kill switch was pressed")
        snapshot = observe()
        counts.append(snapshot.node_count)
        if len(counts) >= 3 and counts[-1] == counts[-2] == counts[-3]:
            return snapshot
        if monotonic() - start >= cap_s:
            return snapshot
        sleep(poll_s)


class SafeTrace:
    """A trace whose write failures are a warning, never a crash: the trace is observability,
    the ledger is safety (RN-39)."""

    def __init__(self, inner: Trace, stderr: TextIO) -> None:
        self._inner = inner
        self._stderr = stderr
        self._warned = False

    def write(self, record: Any) -> None:
        try:
            self._inner.write(record)
        except OSError as exc:
            if not self._warned:
                self._warned = True
                self._stderr.write(
                    f"warning: trace write failed ({exc}); later records are dropped\n"
                )


# ---- the command line ----------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # argparse exits 2 by default; usage errors are 1
        raise UsageError(message)


def _hwnd(text: str) -> int:
    try:
        value = int(text, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("hwnd must be an integer (decimal or 0x...)") from exc
    if value <= 0 or value >= 1 << 64:
        raise argparse.ArgumentTypeError("hwnd must be a positive window handle")
    return value


def _non_negative(kind: type[int] | type[float]) -> Callable[[str], float]:
    def convert(text: str) -> float:
        try:
            value = kind(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"expected a {kind.__name__}") from exc
        if value < 0:
            raise argparse.ArgumentTypeError("must not be negative")
        return value

    return convert


def parse_args(argv: Sequence[str]) -> Options:
    parser = _Parser(
        prog="python -m steerai.run",
        description="Run a plan file against one window; asks reach you on stderr, the result "
        "is one JSON line on stdout. Candidate window titles are printed on stderr.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan", type=Path, help="plan file (JSON, see design 6.3)")
    source.add_argument(
        "--goal", help="plan from this goal first (the plan is written next to the ledger)"
    )
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--window", help="title substring of the one window to act on")
    which.add_argument("--hwnd", type=_hwnd, help="the window handle instead of a title")
    parser.add_argument("--task-id", default=None, help="resume a task; default: a new id")
    parser.add_argument(
        "--ledger-dir",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", ".")) / "steer" / "ledger",
        help="where write-ahead records live (DPAPI-encrypted)",
    )
    parser.add_argument(
        "--trace", type=Path, default=None, help="append a redacted JSON-lines trace"
    )
    parser.add_argument(
        "--max-risk",
        choices=RISKS,
        default="R3",
        help="refuse a plan with any step above this effective tier (R1 = read-only run)",
    )
    parser.add_argument("--max-steps", type=_non_negative(int), default=DEFAULT_MAX_STEPS)
    parser.add_argument("--verify-deadline-s", type=_non_negative(float), default=3.0)
    parser.add_argument("--warmup-s", type=_non_negative(float), default=DEFAULT_WARMUP_S)
    parser.add_argument("--allow-plan-change", action="store_true")
    parser.add_argument("--allow-window-change", action="store_true")
    parser.add_argument(
        "--kill-key",
        default=DEFAULT_KILL_KEY,
        help="the global kill chord (at least one modifier; never F12 or a blocked shortcut)",
    )
    try:
        args = parser.parse_args(list(argv))
    except argparse.ArgumentError as exc:
        raise UsageError(str(exc)) from exc
    try:
        parse_kill_key(args.kill_key)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    task_id = args.task_id or f"task-{uuid.uuid4().hex[:12]}"
    goal = None if args.goal is None else _usable_goal(args.goal)
    plan_path = args.plan if goal is None else Path(args.ledger_dir) / f"{task_id}.plan.json"
    return Options(
        plan=plan_path,
        goal=goal,
        window=args.window,
        hwnd=args.hwnd,
        task_id=task_id,
        ledger_dir=args.ledger_dir,
        trace=args.trace,
        max_risk=args.max_risk,
        max_steps=int(args.max_steps),
        verify_deadline_s=float(args.verify_deadline_s),
        warmup_s=float(args.warmup_s),
        allow_plan_change=bool(args.allow_plan_change),
        allow_window_change=bool(args.allow_window_change),
        kill_key=str(args.kill_key),
    )


# ---- the run -------------------------------------------------------------------------------


class _Out:
    def __init__(self, options: Options, stdout: TextIO, stderr: TextIO) -> None:
        self._options = options
        self._stdout = stdout
        self._stderr = stderr

    def note(self, text: str) -> None:
        # plan errors quote plan text and candidates quote other processes' titles (RN-05)
        self._stderr.write(escape_text(text) + "\n")

    def resume_hint(self) -> None:
        o = self._options
        self.note(
            f"resume: python -m steerai.run --plan {o.plan} --task-id {o.task_id} "
            f"--ledger-dir {o.ledger_dir}"
        )

    def emit(self, payload: dict[str, Any]) -> None:
        self._stdout.write(
            json.dumps({"task_id": self._options.task_id, **payload}, sort_keys=True) + "\n"
        )

    def error(self, detail: str, *, code: ErrorCode | None = None, note: str | None = None) -> int:
        if note is not None:
            self.note(note)
        self.emit({"status": "error", "code": code.value if code else None, "detail": detail})
        return EXIT_USAGE


def _ledger_detail(exc: LedgerError) -> str:
    token = exc.message.split(":", 1)[0].strip()
    return token if token in _LEDGER_TOKENS else "wal_unavailable"


def _candidates(out: _Out, candidates: Sequence[WindowInfo]) -> None:
    if not candidates:
        out.note("no visible window outside this console")
    for w in candidates:
        flag = " (minimized)" if w.minimized else ""
        out.note(f"  hwnd 0x{w.hwnd:x} pid {w.pid}{flag}: {w.title}")


def _hold_refusal(out: _Out, held: str) -> int:
    """The runner's own desktop action was refused by the monitor before it happened."""
    if held == "killed":
        out.emit(_refused("blocked", ErrorCode.GATE_BLOCKED, "killed"))
        return EXIT_BLOCKED
    if held == "takeover":
        out.resume_hint()
        out.emit(_refused("paused", ErrorCode.GATE_PENDING_USER, "takeover"))
        return EXIT_PAUSED
    return out.error(held, code=ErrorCode.GATE_BLOCKED, note=f"monitor: {held}")


def _refused(status: str, code: ErrorCode, detail: str) -> dict[str, Any]:
    return {"status": status, "code": code.value, "detail": detail, "ticked": [], "results": []}


def _result_payload(result: TaskResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "code": result.code.value if result.code else None,
        "detail": result.evidence.get("detail"),
        "ticked": sorted(result.ticked),
        "results": [r.to_dict() for r in result.results],
    }


def _pick_window(options: Options, deps: Deps) -> WindowInfo:
    windows = deps.list_windows()
    excluded_hwnds, excluded_pids = deps.exclusions()
    if options.hwnd is not None:
        listed = [w for w in windows if w.hwnd == options.hwnd]
        if not listed:
            raise RunnerError(ErrorCode.ELEMENT_NOT_FOUND, "no_match", windows)
        window = listed[0]
        if window.hwnd in excluded_hwnds or window.pid in excluded_pids:
            raise UsageError("--hwnd names this runner's own console")
        if window.minimized:
            raise RunnerError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "minimized", listed)
        return window
    if options.window is None:  # the parser requires one of the two
        raise UsageError("--window or --hwnd is required")
    return resolve_window(
        windows, options.window, excluded_hwnds=excluded_hwnds, excluded_pids=excluded_pids
    )


def run(options: Options, deps: Deps, *, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    out = _Out(options, stdout, stderr)
    try:
        return _run(plan_options=options, deps=deps, out=out, stdin=stdin, stderr=stderr)
    except KeyboardInterrupt:
        return _interrupted(out)


def _interrupted(out: _Out) -> int:
    out.note("interrupted; the task can resume")
    out.resume_hint()
    out.emit(
        {
            "status": "paused",
            "code": ErrorCode.GATE_PENDING_USER.value,
            "detail": "interrupted",
            "ticked": [],
            "results": [],
        }
    )
    return EXIT_PAUSED


def _run(*, plan_options: Options, deps: Deps, out: _Out, stdin: TextIO, stderr: TextIO) -> int:
    if plan_options.goal is not None:
        return _run_from_goal(plan_options, deps=deps, out=out, stdin=stdin, stderr=stderr)
    return _run_from_plan(plan_options, deps=deps, out=out, stdin=stdin, stderr=stderr)


def _run_from_plan(
    options: Options, *, deps: Deps, out: _Out, stdin: TextIO, stderr: TextIO
) -> int:
    try:
        data = options.plan.read_bytes()
    except OSError as exc:
        return out.error("plan_unreadable", note=f"plan: {exc}")
    try:
        plan = plan_from_bytes(data)
    except PlanError as exc:
        return out.error("invalid_plan", note=f"plan: {exc}")
    offending = over_limit(plan, options.max_risk)
    if offending is not None:
        return _risk_refusal(out, options, offending)
    try:
        ledger = deps.open_ledger(options.ledger_dir, options.task_id)
    except LedgerError as exc:
        return out.error(
            _ledger_detail(exc), code=ErrorCode.GATE_BLOCKED, note=f"ledger: {exc.message}"
        )
    except OSError as exc:
        return out.error(
            "wal_unavailable",
            code=ErrorCode.GATE_BLOCKED,
            note=f"ledger: {options.ledger_dir} cannot be used: {exc}",
        )
    try:
        return _run_bound(
            plan,
            data,
            ledger=ledger,
            options=options,
            deps=deps,
            out=out,
            stdin=stdin,
            stderr=stderr,
        )
    finally:
        ledger.close()


def _open_trace(options: Options) -> Trace:
    if options.trace is None:
        return NullTrace()
    try:
        trace = JsonLinesTrace(options.trace)
        trace.write({"kind": "run", "task_id": options.task_id})
    except OSError as exc:
        raise UsageError(f"--trace {options.trace}: {exc}") from exc
    return trace


def _run_bound(  # noqa: PLR0913 - the run's state, one return per exit path
    plan: Plan | None,
    data: bytes | None,
    *,
    ledger: LedgerLike,
    options: Options,
    deps: Deps,
    out: _Out,
    stdin: TextIO,
    stderr: TextIO,
) -> int:
    try:
        trace = _open_trace(options)  # first: a bad path binds nothing and taps nothing
        if data is not None:
            ledger.bind_plan(_digest(data), allow_change=options.allow_plan_change)
        monitor = _started_monitor(deps, options)
    except UsageError as exc:
        return out.error("usage", note=str(exc))
    except LedgerError as exc:
        return _ledger_refusal(out, exc)
    except SteerError as exc:
        return _monitor_refusal(out, exc)
    except KeyboardInterrupt:
        return _interrupted(out)
    try:
        return _run_monitored(
            plan,
            ledger=ledger,
            monitor=monitor,
            trace=trace,
            options=options,
            deps=deps,
            out=out,
            stdin=stdin,
            stderr=stderr,
        )
    finally:
        if monitor is not None:
            monitor.close()


def _started_monitor(deps: Deps, options: Options) -> Any:
    if deps.start_monitor is None:
        return None
    try:
        return deps.start_monitor(options)
    except SteerError:
        raise
    except Exception as exc:  # a thread or Win32 failure is still a refusal
        raise SteerError(
            ErrorCode.GATE_BLOCKED, f"monitor_unavailable: {type(exc).__name__}"
        ) from exc


def _monitor_refusal(out: _Out, exc: SteerError) -> int:
    kind = exc.message.split(":", 1)[0].strip()
    if kind not in ("kill_key_unavailable", "monitor_unavailable"):
        kind = "setup_failed"
    hint = " (pass --kill-key to choose another chord)" if kind == "kill_key_unavailable" else ""
    return out.error(kind, code=exc.code, note=f"monitor: {exc.message}{hint}")


def _ledger_refusal(out: _Out, exc: LedgerError) -> int:
    detail = _ledger_detail(exc)
    hint = {
        "plan_changed": " (pass --allow-plan-change if that is deliberate)",
        "window_changed": " (pass --allow-window-change if that is deliberate)",
    }.get(detail, "")
    return out.error(detail, code=ErrorCode.GATE_BLOCKED, note=f"ledger: {exc.message}{hint}")


def _run_monitored(  # noqa: PLR0913, PLR0911 - the run's state, one return per exit path
    plan: Plan | None,
    *,
    ledger: LedgerLike,
    monitor: Any,
    trace: Trace,
    options: Options,
    deps: Deps,
    out: _Out,
    stdin: TextIO,
    stderr: TextIO,
) -> int:
    try:
        window = _pick_window(options, deps)
        ledger.bind_window(
            pid=window.pid, title=window.title, allow_change=options.allow_window_change
        )
        out.note(f"task {options.task_id}: window 0x{window.hwnd:x} pid {window.pid}")
        out.resume_hint()
        planned: Plan | int = (
            plan
            if plan is not None
            else _planned(
                window,
                ledger=ledger,
                trace=trace,
                options=options,
                deps=deps,
                out=out,
                confirmer=ConsoleConfirmer(stdin, stderr, monitor=monitor),
            )
        )
        if isinstance(planned, int):
            return planned
        plan = planned
        if monitor is not None:
            held = wait_for_idle(monitor, monotonic=deps.monotonic, sleep=deps.sleep)
            refused = None if held is None else _hold_refusal(out, held)
            if refused is not None:
                return refused
        try:
            deps.acquire_foreground(window.hwnd)
        except OSError as exc:
            raise RunnerError(ErrorCode.FOCUS_LOST, "foreground") from exc
        bound = Bound(
            hwnd=window.hwnd,
            pid=window.pid,
            title=window.title,
            task_id=options.task_id,
            options=options,
            confirmer=ConsoleConfirmer(stdin, stderr, monitor=monitor),
            ledger=ledger,
            trace=SafeTrace(trace, stderr),
            monitor=monitor,
        )
        session = deps.build_session(bound)
        result = session.run_task(plan)
    except UsageError as exc:
        return out.error("usage", note=str(exc))
    except LedgerError as exc:
        return _ledger_refusal(out, exc)
    except RunnerError as exc:
        _candidates(out, exc.candidates)
        return out.error(exc.detail, code=exc.code, note=f"window: {exc.detail}")
    except SteerError as exc:
        return out.error("setup_failed", code=exc.code, note=f"setup: {exc.message}")
    except KeyboardInterrupt:
        return _interrupted(out)
    except Exception:  # noqa: BLE001 - the last line of defence maps to one exit code
        for line in traceback.format_exc().splitlines():
            out.note(line)  # escaped: exception text can quote titles or plan text
        out.emit({"status": "error", "code": None, "detail": "internal_error"})
        return EXIT_ERROR
    if result.status != "done":
        out.resume_hint()
    out.emit(_result_payload(result))
    return _STATUS_EXIT[result.status]


# ---- from a goal (5.3 [v1.8]) ----------------------------------------------------------------

MAX_GOAL_CHARS = 2000
GO_ID_CHARS = 12


def _usable_goal(goal: str) -> str:
    text = " ".join(goal.split())
    if not text:
        raise UsageError("--goal must not be empty")
    if len(text) > MAX_GOAL_CHARS:
        raise UsageError(f"--goal is longer than {MAX_GOAL_CHARS} characters")
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in text):
        raise UsageError("--goal holds a control character")
    return text


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _risk_refusal(out: _Out, options: Options, offending: Step) -> int:
    out.note(
        f"refused: step {offending.id} is {effective_risk(offending)}, over --max-risk "
        f"{options.max_risk}; nothing was run"
    )
    if options.goal is not None:
        out.note(
            f"the plan is written; to run it: python -m steerai.run --plan {options.plan} "
            f"--task-id {options.task_id} --ledger-dir {options.ledger_dir} "
            f"--max-risk {effective_risk(offending)}"
        )
    out.emit(
        {
            "status": "blocked",
            "code": ErrorCode.GATE_BLOCKED.value,
            "detail": "risk_over_limit",
            "step_id": offending.id,
            "risk": effective_risk(offending),
            "ticked": [],
            "results": [],
        }
    )
    return EXIT_BLOCKED


def _run_from_goal(
    options: Options, *, deps: Deps, out: _Out, stdin: TextIO, stderr: TextIO
) -> int:
    """`--goal`: the plan file is written by the planner after the window is bound, so the
    ledger opens first and the plan digest is bound once the bytes exist (PL-24/32)."""
    if options.plan.exists():
        return out.error(
            "plan_exists",
            note=f"plan: {options.plan} already exists; resume it with --plan {options.plan} "
            f"--task-id {options.task_id} --ledger-dir {options.ledger_dir}",
        )
    try:
        ledger = deps.open_ledger(options.ledger_dir, options.task_id)
    except LedgerError as exc:
        return out.error(
            _ledger_detail(exc), code=ErrorCode.GATE_BLOCKED, note=f"ledger: {exc.message}"
        )
    except OSError as exc:
        return out.error(
            "wal_unavailable",
            code=ErrorCode.GATE_BLOCKED,
            note=f"ledger: {options.ledger_dir} cannot be used: {exc}",
        )
    try:
        return _run_bound(
            None,
            None,
            ledger=ledger,
            options=options,
            deps=deps,
            out=out,
            stdin=stdin,
            stderr=stderr,
        )
    finally:
        ledger.close()


def _planned(  # noqa: PLR0913 - the run's state at planning time
    window: WindowInfo,
    *,
    ledger: LedgerLike,
    trace: Trace,
    options: Options,
    deps: Deps,
    out: _Out,
    confirmer: ConsoleConfirmer,
) -> Plan | int:
    """Observe, plan, write the exact bytes, bind their digest, apply the limit, preview, and
    wait for `go:<id>`; an int is the exit code of a refusal on the way."""
    if deps.screen is None or deps.planner is None or options.goal is None:
        raise SteerError(ErrorCode.GATE_BLOCKED, "no planner configured for --goal")
    title, lines = deps.screen(window.hwnd)
    out.note(f"planning from {len(lines)} screen lines; the model call may take a while")
    request = PlanRequest(goal=options.goal, screen_lines=lines, window_title=title)
    started = deps.monotonic()
    try:
        outcome = deps.planner(request)
    except PlannerError as exc:
        trace.write(exc.trace_record(latency_ms=_ms(deps, started)))
        where = "" if exc.path is None else f" at {exc.path}"
        return out.error(exc.detail, code=exc.code, note=f"planner: {exc.detail}{where}")
    trace.write(outcome.trace_record())
    _write_atomic(options.plan, outcome.data)
    ledger.bind_plan(_digest(outcome.data), allow_change=options.allow_plan_change)
    offending = over_limit(outcome.plan, options.max_risk)
    if offending is not None:
        return _risk_refusal(out, options, offending)
    go_id = _digest(outcome.data)[:GO_ID_CHARS]
    for line in preview_lines(outcome.plan, go_id):
        out.note(line)
    answer = confirmer.ask_go(go_id)
    if answer == "go":
        return outcome.plan
    return _go_refusal(out, answer)


def _ms(deps: Deps, started: float) -> float:
    return (deps.monotonic() - started) * 1000


def _write_atomic(path: Path, data: bytes) -> None:
    """Whole or absent on a process crash (PL-04); no fsync, so a power loss may still lose
    the file, which the ledger's missing digest then reports as a plan to re-plan."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".part")
    temp.write_bytes(data)
    temp.replace(path)


def preview_lines(plan: Plan, go_id: str) -> list[str]:
    """What the person approves: the goal, every condition, every step with its effective
    tier, text and params (a hijacked text must be visible, PL-01/02)."""
    lines = [f"plan {go_id}: {plan.goal}"]
    for subgoal in plan.subgoals:
        lines.append(
            f"  subgoal {subgoal.id}: {subgoal.desc} when {_predicate_text(subgoal.condition)}"
        )
    for step in plan.steps:
        target = step.target
        where = "" if target is None else f' {target.role} "{target.name}"'
        if target is not None and target.match == "person":
            where += " (person)"
        line = f"  step {step.id}: {step.verb}{where} {effective_risk(step)}"
        if step.text is not None:
            line += f" text={step.text!r}"
        if step.params:
            line += f" params={json.dumps(dict(step.params), sort_keys=True)}"
        if step.expect is not None:
            line += f" expect={_predicate_text(step.expect)}"
        lines.append(line)
    lines.append(f"type go:{go_id} to run this plan, or no")
    return lines


def _predicate_text(predicate: Any) -> str:
    return json.dumps(encode_predicate(predicate), sort_keys=True)


def _go_refusal(out: _Out, answer: str) -> int:
    if answer == "killed":
        out.emit(
            {
                "status": "blocked",
                "code": ErrorCode.GATE_BLOCKED.value,
                "detail": "killed",
                "ticked": [],
                "results": [],
            }
        )
        return EXIT_BLOCKED
    if answer == "no":
        out.note("plan declined; nothing was run")
        out.emit(
            {
                "status": "blocked",
                "code": ErrorCode.GATE_BLOCKED.value,
                "detail": "plan_declined",
                "ticked": [],
                "results": [],
            }
        )
        return EXIT_BLOCKED
    out.note("plan not approved (only the exact go token counts); nothing was run")
    out.resume_hint()
    out.emit(
        {
            "status": "paused",
            "code": ErrorCode.GATE_PENDING_USER.value,
            "detail": "plan_unanswered",
            "ticked": [],
            "results": [],
        }
    )
    return EXIT_PAUSED


# ---- the real collaborators ----------------------------------------------------------------


def _build_session(bound: Bound) -> SessionLike:
    from steerai.action.execution import UiaBindings  # noqa: PLC0415 - Windows-only COM setup
    from steerai.action.executor import Executor  # noqa: PLC0415
    from steerai.action.live_reads import UiaLiveReads  # noqa: PLC0415
    from steerai.action.physical import PhysicalExecutor  # noqa: PLC0415
    from steerai.orchestrator.gate import Gate  # noqa: PLC0415
    from steerai.orchestrator.loop import Session  # noqa: PLC0415
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415
    from steerai.probes.windows import list_top_windows  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    client = UiaClient()
    if not client.dpi_per_monitor_v2:
        raise SteerError(
            ErrorCode.PERCEPTION_DEGRADED, "the process is not Per-Monitor-V2 DPI aware"
        )
    bindings = UiaBindings.load()
    model = WorldModel(client)
    monitor = bound.monitor
    blocker = None if monitor is None else _kill_blocker(monitor)
    warm_up(
        lambda: model.observe(bound.hwnd, cap_s=5.0),
        cap_s=bound.options.warmup_s,
        kill=None if monitor is None else monitor.kill_requested,
    )
    return Session(
        task_id=bound.task_id,
        hwnd=bound.hwnd,
        observer=model,
        semantic=Executor(blocker=blocker),
        physical=PhysicalExecutor(blocker=blocker),
        gate=Gate(confirmer=bound.confirmer, own_pid=os.getpid(), task_id=bound.task_id),
        ledger=bound.ledger,
        live=UiaLiveReads(client, bindings),
        windows=list_top_windows,
        trace=bound.trace,
        verify_deadline_s=bound.options.verify_deadline_s,
        max_steps=bound.options.max_steps,
        monitor=monitor,
    )


def _kill_blocker(monitor: Any) -> Callable[[], str | None]:
    return lambda: "killed" if monitor.kill_requested() else None


def _screen(hwnd: int) -> tuple[str, Sequence[str]]:
    """A warm-up observe of the bound window: the title and the 6.2 lines for the planner."""
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    model = WorldModel(UiaClient())
    holder: list[Snapshot] = []

    def observe() -> Snapshot:
        snapshot = model.observe(hwnd, cap_s=5.0)
        holder.append(snapshot)
        return snapshot

    warm_up(observe, cap_s=DEFAULT_WARMUP_S)
    snapshot = holder[-1] if holder else model.observe(hwnd, cap_s=5.0)
    return snapshot.title, snapshot.lines


def _planner(request: PlanRequest) -> PlanOutcome:
    from steerai.planner.claude_planner import ClaudePlanner  # noqa: PLC0415 - the SDK

    return ClaudePlanner.from_env().plan(request)


def _start_monitor(options: Options) -> Any:
    from steerai.session.monitor import HookMonitor  # noqa: PLC0415 - Windows hooks

    monitor = HookMonitor(kill_key=options.kill_key)
    monitor.start()
    return monitor


def _real_deps() -> Deps:
    from steerai.orchestrator.ledger import Ledger  # noqa: PLC0415 - DPAPI, Windows only
    from steerai.probes.win_input import acquire_foreground  # noqa: PLC0415
    from steerai.probes.windows import console_exclusions, list_top_windows  # noqa: PLC0415

    return Deps(
        list_windows=list_top_windows,
        exclusions=console_exclusions,
        acquire_foreground=acquire_foreground,
        open_ledger=lambda root, task_id: Ledger(root, task_id=task_id, started_at=time.time()),
        build_session=_build_session,
        start_monitor=_start_monitor,
        screen=_screen,
        planner=_planner,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        options = parse_args(sys.argv[1:] if argv is None else argv)
    except UsageError as exc:
        sys.stderr.write(f"usage error: {exc}\n")
        sys.stdout.write(json.dumps({"status": "error", "code": None, "detail": "usage"}) + "\n")
        return EXIT_USAGE
    return run(options, _real_deps(), stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
