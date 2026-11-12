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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, TextIO

from steerai import model_calls
from steerai.errors import ErrorCode, SteerError
from steerai.goal_match import GoalMatcher, matcher_id
from steerai.memory.plan_cache import CacheKey, Entry, PlanCache, outcome_of, refusal
from steerai.orchestrator.console import ConsoleConfirmer, escape_text
from steerai.orchestrator.contracts import (
    OPENED_PREFIX,
    RISKS,
    START_WINDOW,
    Plan,
    Risk,
    Step,
    subgoal_verbs,
)
from steerai.orchestrator.gate import Confirmer, effective_risk, open_tier
from steerai.orchestrator.ledger import LedgerError
from steerai.orchestrator.loop import DEFAULT_MAX_STEPS, NO_EFFECT, TaskResult, wait_for_idle
from steerai.orchestrator.plan_io import PlanError, encode_predicate, plan_from_bytes
from steerai.orchestrator.replan import (
    MAX_REPLANS,
    Progress,
    continuation_refusal,
    replan_trigger,
)
from steerai.orchestrator.trace import JsonLinesTrace, NullTrace, Trace
from steerai.planner.contracts import PlannerError, PlanOutcome, PlanRequest
from steerai.probes.windows import WindowInfo
from steerai.session.monitor import DEFAULT_KILL_KEY, parse_kill_key
from steerai.world.elements import safe_name
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
PLAN_QUIET_S = 2.0
"""How long a screen the planner reads must keep its node count ([v2.80]). Measured 2026-11-12
(Chrome 157, n=3, an Enter search on YouTube, a UIA observe every 100 ms): the tree holds a
91-node skeleton for 0.72-0.94 s before the results land (1.2-2.8 s after the Enter), so three
equal counts 0.25 s apart can return the skeleton; doubled, as QUIET_AFTER_TYPE_S. Other pages
unmeasured."""
_LEDGER_TOKENS = frozenset(
    {
        "ledger_locked",
        "plan_changed",
        "plan_chained",
        "plan_pending",
        "plan_superseded",
        "plan_repeated",
        "plan_unbound",
        "step_id_reused",
        "window_changed",
    }
)
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
    desktop: bool = False
    """Start on the shell's desktop window (a task that begins with an `open`)."""

    def replace(self, **changes: Any) -> Options:
        return dataclasses.replace(self, **changes)


class LedgerLike(Protocol):
    def bind_plan(
        self,
        digest: str,
        *,
        allow_change: bool = False,
        step_ids: Sequence[str] = (),
        subgoal_verbs: Mapping[str, Sequence[str]] | None = None,
    ) -> None: ...
    def propose_plan(
        self,
        digest: str,
        step_ids: Sequence[str],
        subgoal_verbs: Mapping[str, Sequence[str]] | None = None,
    ) -> None: ...
    def extend_plan(
        self,
        digest: str,
        step_ids: Sequence[str],
        subgoal_verbs: Mapping[str, Sequence[str]] | None = None,
    ) -> None: ...
    def plan_count(self) -> int: ...
    def unsettled(self) -> bool: ...
    def bind_window(
        self,
        *,
        pid: int,
        title: str,
        allow_change: bool = False,
        identity: Mapping[str, Any] | None = None,
    ) -> None: ...
    def close(self) -> str | None: ...


class GoConfirmer(Confirmer, Protocol):
    """A confirmer that also answers the plan preview (`go` / `no` / `killed` / `unanswered`)."""

    def ask_go(self, go_id: str) -> str: ...


ConfirmerFactory = Callable[[TextIO, TextIO, Any], GoConfirmer]
"""(stdin, stderr, monitor) -> the confirmer for this run: the console by default, the
overlay's broker from the app (packet launcher-2026-11-04)."""


def _console_confirmer(stdin: TextIO, stderr: TextIO, monitor: Any) -> GoConfirmer:
    return ConsoleConfirmer(stdin, stderr, monitor=monitor)


class SessionLike(Protocol):
    hwnd: int
    """The window the task is in now: the opened one after an `open`."""

    def run_task(self, plan: Plan, *, continuation: bool = False) -> TaskResult: ...
    def live_title(self, ref: str) -> str | None: ...
    def no_effect_holds(self) -> bool: ...


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
class ScreenRead:
    """A screen the planner reads: the window's title, its 6.2 lines, and whether the quiet read
    ended on the cap rather than a quiet window (#233: the capped rate is traced)."""

    title: str
    lines: Sequence[str]
    capped: bool


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
    desktop: Callable[[], WindowInfo] | None = None
    """The shell's desktop window for `--desktop`; None only in pure tests."""
    open_windows: Callable[[], Sequence[str]] | None = None
    """Titles of the open windows of registry apps, for the planner; None only in pure tests."""
    screen: Callable[[int, Callable[[], bool] | None], ScreenRead] | None = None
    """(hwnd, kill) -> the window's title and 6.2 lines after a quiet read ([v2.80]), the kill
    switch checked between observes (TK-22, #233); None only in pure tests."""
    planner: Callable[[PlanRequest], PlanOutcome] | None = None
    """The LLM planner (5.3); None only in pure tests or a `--plan` run."""
    identify: Callable[[int], Mapping[str, Any] | None] | None = None
    """A window's identity (`window_identity`), recorded for `window: start`; None only in
    pure tests."""
    session_locked: Callable[[], bool | None] | None = None
    """The session's lock state (issue #31); None only in pure tests."""
    make_confirmer: ConfirmerFactory = _console_confirmer
    plan_cache: PlanCache | None = None
    """The verified plan cache (5.12 [v2.33]); None turns it off (pure tests, no LOCALAPPDATA)."""
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    """Starts the takeover monitor (hooks and the kill hot key) for these options; None only in
    pure tests. The real runner always monitors: 5.1 has no tier exception."""

    def replace(self, **changes: Any) -> Deps:
        return dataclasses.replace(self, **changes)


# ---- pure parts --------------------------------------------------------------------------


def plan_tier(plan: Plan, step: Step) -> Risk:
    """A step's tier as far as the plan alone decides it: floors and deny patterns, and for an
    `open` the tier its content gives (`open_tier`, page content is R2). The screen can only
    raise it further at run time, where the session enforces the same limit (#88)."""
    if step.verb == "open":
        step = replace(step, risk=open_tier(plan.goal, step))
    return effective_risk(step)


def over_limit(plan: Plan, max_risk: Risk) -> Step | None:
    """The first step whose tier is over the limit, checked over the whole plan before anything
    runs (RN-02, IJ-28)."""
    for step in plan.steps:
        if _RISK_ORDER[plan_tier(plan, step)] > _RISK_ORDER[max_risk]:
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
    # an unreadable twin still counts: dropping it would turn ambiguity into a pick (CU-05)
    if len(matches) > 1:
        raise RunnerError(ErrorCode.ELEMENT_NOT_FOUND, "ambiguous_target", matches)
    if matches[0].minimized:
        raise RunnerError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "minimized", matches)
    if matches[0].cloak_unreadable:
        raise RunnerError(ErrorCode.WINDOW_OTHER_DESKTOP, "cloak_unreadable", matches)
    return matches[0]


def warm_up(  # noqa: PLR0913 - keyword-only tuning knobs plus the injected clock and kill
    observe: Callable[[], Snapshot],
    *,
    cap_s: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = 0.25,
    kill: Callable[[], bool] | None = None,
    quiet_s: float | None = None,
) -> Snapshot:
    """First contact (5.1 [v0.6], RN-18): observe until three consecutive snapshots agree on
    the node count, or, with `quiet_s`, until the node count has not changed, every walk
    stable and every observe answered, for `quiet_s` ([v2.80]: a screen the planner reads); or
    the cap passes; the last snapshot is returned
    either way. Bounded by polls too, so a clock that never moves still ends. A provider
    that answers `perception_timeout` for a moment (E_FAIL right after a foreground change,
    overlay runs 2026-11-04) is "not yet": polling continues until the cap, and the cap with
    no answer at all raises the last error. The kill switch is honoured between observes
    (TK-22)."""
    return warm_up_read(
        observe,
        cap_s=cap_s,
        monotonic=monotonic,
        sleep=sleep,
        poll_s=poll_s,
        kill=kill,
        quiet_s=quiet_s,
    ).snapshot


@dataclass(frozen=True, slots=True)
class WarmRead:
    """A warm-up's last snapshot, and whether the cap (or the poll bound) ended it rather than
    the agreeing counts or the quiet window (#233: a capped plan read is traced)."""

    snapshot: Snapshot
    capped: bool


def warm_up_read(  # noqa: PLR0913 - warm_up's knobs
    observe: Callable[[], Snapshot],
    *,
    cap_s: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = 0.25,
    kill: Callable[[], bool] | None = None,
    quiet_s: float | None = None,
) -> WarmRead:
    """`warm_up`, telling whether it ended on the cap."""
    _check_warm_up(quiet_s, poll_s)
    start = monotonic()
    counts: list[int] = []
    changed_at = start
    snapshot: Snapshot | None = None
    failure: SteerError | None = None
    for _ in range(int(cap_s / poll_s) + 2):
        if kill is not None and kill():
            raise SteerError(ErrorCode.GATE_BLOCKED, "killed: the kill switch was pressed")
        try:
            snapshot = observe()
        except SteerError as exc:
            if exc.code is not ErrorCode.PERCEPTION_TIMEOUT:
                raise
            failure = exc
            changed_at = monotonic()  # "not yet" is never quiet time (#232 review item 1)
        else:
            if not counts or counts[-1] != snapshot.node_count or not snapshot.stable:
                # a tree changing during the walk is not quiet either, as `Session._quiet`
                changed_at = monotonic()
            counts.append(snapshot.node_count)
            if _warm(counts, quiet_s, monotonic() - changed_at):
                return WarmRead(snapshot, capped=False)
        if monotonic() - start >= cap_s:
            break
        sleep(poll_s)
    # the cap, or the poll bound under a clock that never moves: the last look either way
    if snapshot is None:
        raise (
            failure
            if failure is not None
            else SteerError(ErrorCode.PERCEPTION_TIMEOUT, "warm-up produced no snapshot")
        )
    return WarmRead(snapshot, capped=True)


def _check_warm_up(quiet_s: float | None, poll_s: float) -> None:
    if quiet_s is not None and quiet_s <= 0:
        raise ValueError("quiet_s must be positive")
    if poll_s <= 0:
        raise ValueError("poll_s must be positive")


def _warm(counts: list[int], quiet_s: float | None, quiet_for: float) -> bool:
    """Three equal node counts in a row (first contact), or quiet for `quiet_s` ([v2.80])."""
    if quiet_s is None:
        return len(counts) >= 3 and counts[-1] == counts[-2] == counts[-3]
    return quiet_for >= quiet_s


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
    which.add_argument(
        "--desktop", action="store_true", help="start on the desktop (the plan opens its apps)"
    )
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
        desktop=bool(args.desktop),
    )


# ---- the run -------------------------------------------------------------------------------


class _Out:
    def __init__(self, options: Options, stdout: TextIO, stderr: TextIO) -> None:
        self._options = options
        self._stdout = stdout
        self._stderr = stderr
        self.plan = options.plan
        """The task's last bound plan file: the one a resume names (replan RP-15/42)."""
        self.cache: _CacheRun | None = None
        """The plan cache's part in this run: its key, and the entry a hit used."""

    def note(self, text: str) -> None:
        # plan errors quote plan text and candidates quote other processes' titles (RN-05)
        self._stderr.write(escape_text(text) + "\n")

    def resume_hint(self) -> None:
        o = self._options
        self.note(
            f"resume: python -m steerai.run --plan {self.plan} --task-id {o.task_id} "
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


def _tap_foreground(options: Options, deps: Deps, window: WindowInfo) -> None:
    """The target in front before the first step; never the shell's desktop (schema gate)."""
    if options.desktop:
        return
    try:
        deps.acquire_foreground(window.hwnd)
    except OSError as exc:
        raise RunnerError(ErrorCode.FOCUS_LOST, "foreground") from exc


def _start_checked(planned: Plan | int, options: Options, out: _Out) -> Plan | int:
    """A plan, or the exit code that refused it: `window: start` on a `--desktop` task."""
    if isinstance(planned, int):
        return planned
    start_at = _desktop_start(planned, options)
    if start_at is None:
        return planned
    return out.error(
        "invalid_plan",
        code=ErrorCode.MODEL_ERROR,
        note=f"plan: {start_at}: a task started on the desktop has no start window to go back "
        "to; nothing was run",
    )


def _desktop_start(plan: Plan, options: Options) -> str | None:
    """`window: start` on a `--desktop` task would act on the shell (step-window SW-05)."""
    if not options.desktop:
        return None
    for index, step in enumerate(plan.steps):
        if step.window == START_WINDOW:
            return f"$.steps[{index}].window"
    return None


def _refuse_locked(deps: Deps) -> None:
    """Nothing is planned, tapped or run on a locked (or unknown) session (issue #31)."""
    if deps.session_locked is not None and deps.session_locked() is not False:
        raise RunnerError(ErrorCode.SECURE_DESKTOP, "session_locked")


def _pick_window(options: Options, deps: Deps) -> WindowInfo:
    if options.desktop:
        if deps.desktop is None:
            raise UsageError("--desktop is not available here")
        try:
            return deps.desktop()
        except OSError as exc:
            raise RunnerError(ErrorCode.ELEMENT_NOT_FOUND, "no_desktop") from exc
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
        if window.cloak_unreadable:
            raise RunnerError(ErrorCode.WINDOW_OTHER_DESKTOP, "cloak_unreadable", listed)
        return window
    if options.window is None:  # the parser requires one of the two
        raise UsageError("--window or --hwnd is required")
    return resolve_window(
        windows, options.window, excluded_hwnds=excluded_hwnds, excluded_pids=excluded_pids
    )


def run(options: Options, deps: Deps, *, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    out = _Out(options, stdout, stderr)
    model_calls.reset()  # one run's calls (a probe runs several in one process)
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
        return _risk_refusal(out, options, plan, offending)
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
        _closed(ledger, out)


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
        if data is not None and plan is not None:
            ledger.bind_plan(
                _digest(data),
                allow_change=options.allow_plan_change,
                step_ids=[s.id for s in plan.steps],
                subgoal_verbs=subgoal_verbs(plan),
            )
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
        _refuse_locked(deps)
        window = _pick_window(options, deps)
        ledger.bind_window(
            pid=window.pid,
            title=window.title,
            allow_change=options.allow_window_change,
            identity=None if deps.identify is None else deps.identify(window.hwnd),
        )
        out.note(f"task {options.task_id}: window 0x{window.hwnd:x} pid {window.pid}")
        out.resume_hint()
        confirmer = deps.make_confirmer(stdin, stderr, monitor)
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
                confirmer=confirmer,
                kill=None if monitor is None else monitor.kill_requested,
            )
        )
        planned = _start_checked(planned, options, out)
        if isinstance(planned, int):
            return planned
        plan = planned
        if monitor is not None:
            held = wait_for_idle(monitor, monotonic=deps.monotonic, sleep=deps.sleep)
            refused = None if held is None else _hold_refusal(out, held)
            if refused is not None:
                return refused
        _tap_foreground(options, deps, window)
        bound = Bound(
            hwnd=window.hwnd,
            pid=window.pid,
            title=window.title,
            task_id=options.task_id,
            options=options,
            confirmer=confirmer,
            ledger=ledger,
            trace=SafeTrace(trace, stderr),
            monitor=monitor,
        )
        session = deps.build_session(bound)
        _preset_cached(out.cache, session, plan)
        # a resumed later link of a replanned task runs as a continuation (hunter RP-01)
        result = session.run_task(plan, continuation=ledger.plan_count() > 1)
        if options.goal is not None:
            replan = Replan(
                session=session,
                start=window,
                ledger=ledger,
                monitor=monitor,
                trace=trace,
                options=options,
                deps=deps,
                out=out,
                confirmer=confirmer,
            )
            result = _replanning(replan, plan, result)
        _settle_cache(
            out.cache, session, plan, result, replanned=ledger.plan_count() > 1, trace=trace
        )
        trace.write({"kind": "model_calls", **model_calls.snapshot()})
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
    if result.status != "done" and result.evidence.get("detail") != "plan_declined":
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


def _risk_refusal(out: _Out, options: Options, plan: Plan, offending: Step) -> int:
    tier = plan_tier(plan, offending)
    out.note(
        f"refused: step {offending.id} is {tier}, over --max-risk "
        f"{options.max_risk}; nothing was run"
    )
    if options.goal is not None:
        out.note(
            f"the plan is written; to run it: python -m steerai.run --plan {options.plan} "
            f"--task-id {options.task_id} --ledger-dir {options.ledger_dir} "
            f"--max-risk {tier}"
        )
    out.emit(
        {
            "status": "blocked",
            "code": ErrorCode.GATE_BLOCKED.value,
            "detail": "risk_over_limit",
            "step_id": offending.id,
            "risk": tier,
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
        _closed(ledger, out)


def _planned(  # noqa: PLR0913 - the run's state at planning time
    window: WindowInfo,
    *,
    ledger: LedgerLike,
    trace: Trace,
    options: Options,
    deps: Deps,
    out: _Out,
    confirmer: GoConfirmer,
    kill: Callable[[], bool] | None,
) -> Plan | int:
    """Observe, plan, write the exact bytes, bind their digest, apply the limit, preview, and
    wait for `go:<id>`; an int is the exit code of a refusal on the way (a kill during the
    read is `killed`, #233)."""
    if deps.screen is None or deps.planner is None or options.goal is None:
        raise SteerError(ErrorCode.GATE_BLOCKED, "no planner configured for --goal")
    screen = _read_screen(deps.screen, window.hwnd, kill, deps, trace)
    if screen is None:
        return _hold_refusal(out, "killed")
    title, lines = screen
    out.note(f"planning from {len(lines)} screen lines; the model call may take a while")
    open_windows = deps.open_windows() if deps.open_windows is not None else ()
    request = PlanRequest(
        goal=options.goal,
        screen_lines=lines,
        window_title=title,
        open_windows=open_windows,
        # the first plan's only window to come back to is the one the task starts in
        windows=() if options.desktop else ((START_WINDOW, title),),
    )
    out.cache = _cache_lookup(window, deps, options, trace, lines)
    hit = out.cache.entry if out.cache is not None else None
    if hit is not None:
        data, plan = hit.plan, plan_from_bytes(hit.plan)
        out.note(f"cached plan ({hit.state}, verified {hit.uses} time(s)); no planner call")
    else:
        started = deps.monotonic()
        try:
            outcome = deps.planner(request)
        except PlannerError as exc:
            trace.write(exc.trace_record(latency_ms=_ms(deps, started)))
            where = "" if exc.path is None else f" at {exc.path}"
            return out.error(exc.detail, code=exc.code, note=f"planner: {exc.detail}{where}")
        trace.write(outcome.trace_record())
        data, plan = outcome.data, outcome.plan
    if out.cache is not None:
        out.cache.data = data
    _write_atomic(options.plan, data)
    ledger.bind_plan(
        _digest(data),
        allow_change=options.allow_plan_change,
        step_ids=[s.id for s in plan.steps],
        subgoal_verbs=subgoal_verbs(plan),
    )
    offending = over_limit(plan, options.max_risk)
    if offending is not None:
        return _risk_refusal(out, options, plan, offending)
    # the task id in the go id: a cached plan's bytes repeat, its approval never does (gate 4)
    go_id = _digest(data + options.task_id.encode())[:GO_ID_CHARS]
    for line in preview_lines(plan, go_id):
        out.note(line)
    answer = confirmer.ask_go(go_id)
    if answer == "go":
        return plan
    return _go_refusal(out, answer)


@dataclass
class _CacheRun:
    cache: PlanCache
    key: CacheKey
    app_kind: str
    entry: Entry | None = None
    """The entry this run replays (a usable hit), or None."""
    data: bytes = b""
    """The bytes of the plan this run bound (written to the cache on a verified success)."""


def _cache_lookup(
    window: WindowInfo, deps: Deps, options: Options, trace: Trace, lines: Sequence[str]
) -> _CacheRun | None:
    """A usable hit: the exact goal, a plan that still loads and may be cached, and its first
    target shown on this screen (validate on use, 5.12). Tokens only in the trace."""
    if deps.plan_cache is None or options.goal is None:
        return None
    kind = _app_kind(deps, window)
    if kind is None:
        return None
    from steerai.planner.claude_planner import PROMPT_VERSION  # noqa: PLC0415 - planner module

    key = CacheKey.of(
        options.goal, app_kind=kind, prompt_version=PROMPT_VERSION, matcher=matcher_id()
    )
    found_run = _CacheRun(deps.plan_cache, key, kind)
    try:
        found = deps.plan_cache.get(key)
    except OSError:
        trace.write({"kind": "cache", "event": "unreadable"})
        return found_run
    event: str = found.event
    if found.entry is not None:
        try:
            cached: Plan | None = plan_from_bytes(found.entry.plan)
        except PlanError:
            cached = None
            event = "corrupt"
        if cached is not None and (
            refusal(cached, options.goal, kind) is not None or not _first_shown(cached, lines)
        ):
            event = "stale"
        elif cached is not None:
            found_run.entry = found.entry
    record: dict[str, Any] = {"kind": "cache", "event": event}
    if found_run.entry is not None:
        record.update(state=found_run.entry.state, uses=found_run.entry.uses)
    trace.write(record)
    return found_run


def _app_kind(deps: Deps, window: WindowInfo) -> str | None:
    record = deps.identify(window.hwnd) if deps.identify is not None else None
    image = (record or {}).get("image")
    return Path(str(image)).name.casefold() if image else None


def _first_shown(plan: Plan, lines: Sequence[str]) -> bool:
    for step in plan.steps:
        if step.verb == "open" or step.target is None:
            continue
        needle = f'{step.target.role} "{safe_name(step.target.name)}"'
        return any(needle in line for line in lines)
    return True


def _preset_cached(cache_run: _CacheRun | None, session: SessionLike, plan: Plan) -> None:
    """A trusted entry's verdicts stand for its own plan; a probation hit is judged live
    (hunter PC-05: a poisoned plan never skips the judge before three verified runs)."""
    if cache_run is None or cache_run.entry is None or cache_run.entry.state != "trusted":
        return
    preset = getattr(session, "preset", None)
    if preset is not None:
        preset(plan, cache_run.entry.verdicts)


def _settle_cache(  # noqa: PLR0913 - the run's end state, passed once
    cache_run: _CacheRun | None,
    session: SessionLike,
    plan: Plan,
    result: TaskResult,
    *,
    replanned: bool,
    trace: Trace,
) -> None:
    """A hit is scored; a fresh plan is written only after a fully verified single-plan run
    whose every step goal-match judged `serves` (gate items 2-3). Never fails the task."""
    if cache_run is None:
        return
    outcome = outcome_of(plan, result.results, replanned=replanned, status=result.status)
    try:
        if cache_run.entry is not None:
            state = cache_run.cache.score(cache_run.entry, outcome)
            trace.write({"kind": "cache", "event": "score", "outcome": outcome, "state": state})
            return
        verdicts, failure = getattr(session, "judged", ({}, "unjudged"))
        if (
            outcome != "success"
            or result.status != "done"
            or replanned
            or failure is not None
            or not verdicts
            or any(v != "serves" for v in verdicts.values())
            or refusal(plan, cache_run.key.goal, cache_run.app_kind) is not None
        ):
            return
        cache_run.cache.put(cache_run.key, cache_run.data, verdicts)
        trace.write({"kind": "cache", "event": "write", "state": "probation"})
    except OSError:
        trace.write({"kind": "cache", "event": "unwritable"})


def _ms(deps: Deps, started: float) -> float:
    return (deps.monotonic() - started) * 1000


def _read_screen(
    screen: Callable[[int, Callable[[], bool] | None], ScreenRead],
    hwnd: int,
    kill: Callable[[], bool] | None,
    deps: Deps,
    trace: Trace,
) -> tuple[str, Sequence[str]] | None:
    """The planner's screen, read under the kill switch, and traced: how long the read took,
    how it ended, and whether it hit the cap ([v2.80] section 9 row, #233). No title, no
    lines. None is a kill during the read: a GATE_BLOCKED with the switch latched (#239
    review: any other GATE_BLOCKED is raised as it came)."""
    started = deps.monotonic()
    try:
        read = screen(hwnd, kill)
    except SteerError as exc:
        killed = exc.code is ErrorCode.GATE_BLOCKED and kill is not None and kill()
        _traced_read(trace, deps, started, outcome="killed" if killed else "raised")
        if killed:
            return None
        raise
    _traced_read(trace, deps, started, outcome="ok", capped=read.capped, lines=len(read.lines))
    return read.title, read.lines


def _traced_read(  # noqa: PLR0913 - one trace record's fields
    trace: Trace,
    deps: Deps,
    started: float,
    *,
    outcome: str,
    capped: bool = False,
    lines: int = 0,
) -> None:
    trace.write(
        {
            "kind": "plan_read",
            "outcome": outcome,
            "read_ms": round(_ms(deps, started)),
            "capped": capped,
            "lines": lines,
        }
    )


def _write_atomic(path: Path, data: bytes) -> None:
    """Whole or absent on a process crash (PL-04); no fsync, so a power loss may still lose
    the file, which the ledger's missing digest then reports as a plan to re-plan."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".part")
    temp.write_bytes(data)
    temp.replace(path)


def preview_lines(
    plan: Plan,
    go_id: str,
    *,
    continuation: int = 0,
    ticked: Sequence[str] = (),
    rewritten: frozenset[str] = frozenset(),
) -> list[str]:
    """What the person approves: the goal, every condition, every step with its effective
    tier, text and params (a hijacked text must be visible, PL-01/02). A continuation names
    its number, the subgoals already reached and the conditions it rewrote (decision Q1)."""
    lines = []
    if continuation:
        reached = ", ".join(ticked) or "none"
        lines.append(f"continuation {continuation}/{MAX_REPLANS}; subgoals reached: {reached}")
    lines.append(f"plan {go_id}: {plan.goal}")
    for subgoal in plan.subgoals:
        mark = " (condition rewritten)" if subgoal.id in rewritten else ""
        lines.append(
            f"  subgoal {subgoal.id}: {subgoal.desc} when "
            f"{_predicate_text(subgoal.condition)}{mark}"
        )
    for step in plan.steps:
        target = step.target
        where = "" if target is None else f' {target.role} "{target.name}"'
        if target is not None and target.match == "person":
            where += " (person)"
        line = f"  step {step.id}: {step.verb}{where} {plan_tier(plan, step)}"
        if step.text is not None:
            line += f" text={step.text!r}"
        if step.params:
            line += f" params={json.dumps(dict(step.params), sort_keys=True)}"
        if step.expect is not None:
            line += f" expect={_predicate_text(step.expect)}"
        if step.window is not None:
            line += f" window={step.window}"  # where it lands (SW-09)
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


# ---- replanning (5.3 [v2.1], packet replan-2026-11-06) --------------------------------------


@dataclass(frozen=True, slots=True)
class Replan:
    """The run's state a replan needs."""

    session: SessionLike
    start: WindowInfo
    ledger: LedgerLike
    monitor: Any
    trace: Trace
    options: Options
    deps: Deps
    out: _Out
    confirmer: GoConfirmer


def _replanning(r: Replan, plan: Plan, result: TaskResult) -> TaskResult:
    """Plan again from the window the task reached while the run's end is on the allow-list,
    at most `MAX_REPLANS` times; every plan is previewed and needs its own `go:<id>`. The
    result is the task's, merged over its plans (schema gate items 1, 6, 7)."""
    progress = Progress()
    progress.add(plan, result)
    number = 0
    while True:
        trigger = replan_trigger(progress.last, progress.steps)
        if trigger is None or not progress.remaining():
            return progress.merged()
        if trigger == "unverified_effect" or r.ledger.unsettled():
            r.out.note(
                "stopped: a step with an effect may have acted unverified; check the screen "
                "before running the task again"
            )
            return progress.ended("stopped", ErrorCode.VERIFY_FAILED, "unverified_effect")
        if number >= MAX_REPLANS:
            r.out.note(f"stopped after {MAX_REPLANS} replans; the goal needs a person to look")
            return progress.ended("stopped", ErrorCode.BUDGET_EXCEEDED, "replan_limit")
        if r.deps.session_locked is not None and r.deps.session_locked() is not False:
            # nothing on screen leaves the machine for a person who is away (review item 2)
            return progress.ended("stopped", ErrorCode.SECURE_DESKTOP, "session_locked")
        number += 1
        r.trace.write(
            {"kind": "replan", "task_id": r.options.task_id, "number": number, "trigger": trigger}
        )
        planned = _continuation_plan(r, progress, number)
        if isinstance(planned, TaskResult):
            return planned
        ready = _ready_for(r, progress)
        if ready is not None:
            return ready
        progress.add(planned, r.session.run_task(planned, continuation=True))


def _continuation_screen(
    r: Replan,
    screen: Callable[[int, Callable[[], bool] | None], ScreenRead],
    progress: Progress,
) -> tuple[str, Sequence[str]] | TaskResult:
    """The replan's read of the window the task reached; a kill during it ends the task
    `killed` (#233), any other failure `observe_raised`."""
    kill = None if r.monitor is None else r.monitor.kill_requested
    try:
        read = _read_screen(screen, r.session.hwnd, kill, r.deps, r.trace)
    except SteerError as exc:
        return progress.ended("stopped", exc.code, "observe_raised")
    if read is None:
        return progress.ended("blocked", ErrorCode.GATE_BLOCKED, "killed")
    return read


def _continuation_plan(r: Replan, progress: Progress, number: int) -> Plan | TaskResult:
    """Observe the current window, plan the remaining subgoals, refuse a plan that breaks the
    continuation rules, then bind, preview and ask (`_bound_continuation`); a TaskResult ends
    the task on the way, with what did run."""
    if r.deps.screen is None or r.deps.planner is None or r.options.goal is None:
        raise SteerError(ErrorCode.GATE_BLOCKED, "no planner configured for --goal")
    screen = _continuation_screen(r, r.deps.screen, progress)
    if isinstance(screen, TaskResult):
        return screen
    title, lines = screen
    if progress.last.evidence.get("detail") == NO_EFFECT and not r.session.no_effect_holds():
        # the facts read again after the replan's observe (NE-08): a page that moved since
        # the verify deadline is an uncertain effect, and no plan acts on top of it
        r.out.note(
            "stopped: the page changed after the submit was judged to have had no effect; "
            "check the screen before running the task again"
        )
        return progress.ended("stopped", ErrorCode.VERIFY_FAILED, "unverified_effect")
    path = r.options.plan.with_suffix(f".{number + 1}.json")
    if path.exists():  # before the model call: a refusal costs nothing (review note)
        r.out.note(f"plan: {path} already exists; nothing was written")
        return progress.ended("stopped", ErrorCode.GATE_BLOCKED, "plan_exists")
    r.out.note(f"replanning ({number}/{MAX_REPLANS}) from {len(lines)} screen lines")
    request = PlanRequest(
        goal=r.options.goal,
        screen_lines=lines,
        window_title=title,
        open_windows=r.deps.open_windows() if r.deps.open_windows is not None else (),
        continuation=number,
        windows=_task_windows(r, progress),
        done=progress.done_steps(),
        remaining=progress.remaining(),
        failure=progress.failure(),
    )
    started = r.deps.monotonic()
    try:
        outcome = r.deps.planner(request)
    except PlannerError as exc:
        r.trace.write(exc.trace_record(latency_ms=_ms(r.deps, started)))
        where = "" if exc.path is None else f" at {exc.path}"
        r.out.note(f"planner: {exc.detail}{where}")
        return progress.ended("stopped", exc.code, exc.detail)
    r.trace.write(outcome.trace_record())
    refusal = continuation_refusal(outcome.plan, progress, desktop=r.options.desktop)
    if refusal is not None:
        r.out.note(f"planner: the continuation breaks a rule at {refusal}; nothing was written")
        return progress.ended("stopped", ErrorCode.MODEL_ERROR, "invalid_plan")
    return _bound_continuation(r, progress, number, outcome)


def _task_windows(r: Replan, progress: Progress) -> list[tuple[str, str]]:
    """The windows a continuation's steps may name, by reference with the live title: the
    start window (never the shell's desktop) and each verified `open` still the task's."""
    refs = [] if r.options.desktop else [START_WINDOW]
    refs += [
        OPENED_PREFIX + result.step_id
        for result in progress.results()
        if result.ok and progress.steps[result.step_id].verb == "open"
    ]
    found = [(ref, r.session.live_title(ref)) for ref in refs]
    return [(ref, title) for ref, title in found if title is not None]


def _bound_continuation(
    r: Replan, progress: Progress, number: int, outcome: PlanOutcome
) -> Plan | TaskResult:
    """Write the exact bytes as `<task>.plan.<n>.json`, bind them as the chain's next link,
    apply the risk limit, preview (decision Q1) and wait for this plan's own `go:<id>`."""
    path = r.options.plan.with_suffix(f".{number + 1}.json")
    verbs = subgoal_verbs(outcome.plan)
    try:
        # the link first, then its file: a crash between them resumes the file as this link
        r.ledger.propose_plan(_digest(outcome.data), [s.id for s in outcome.plan.steps], verbs)
    except LedgerError as exc:
        r.out.note(f"ledger: {exc.message}")
        return progress.ended("blocked", ErrorCode.GATE_BLOCKED, _ledger_detail(exc))
    _write_atomic(path, outcome.data)
    try:
        r.ledger.extend_plan(
            _digest(outcome.data),
            [s.id for s in outcome.plan.steps],
            subgoal_verbs(outcome.plan),
        )
    except LedgerError as exc:
        r.out.note(f"ledger: {exc.message}")
        return progress.ended("blocked", ErrorCode.GATE_BLOCKED, _ledger_detail(exc))
    r.out.plan = path
    offending = over_limit(outcome.plan, r.options.max_risk)
    if offending is not None:
        r.out.note(
            f"refused: step {offending.id} is {plan_tier(outcome.plan, offending)}, over "
            f"--max-risk {r.options.max_risk}; the continuation was not run"
        )
        return progress.ended("blocked", ErrorCode.GATE_BLOCKED, "risk_over_limit")
    go_id = _digest(outcome.data + r.options.task_id.encode())[:GO_ID_CHARS]
    preview = preview_lines(
        outcome.plan,
        go_id,
        continuation=number,
        ticked=sorted(progress.ticked),
        rewritten=frozenset(progress.rewritten(outcome.plan)),
    )
    for line in preview:
        r.out.note(line)
    answer = r.confirmer.ask_go(go_id)
    return outcome.plan if answer == "go" else _not_approved(r, progress, answer)


def _not_approved(r: Replan, progress: Progress, answer: str) -> TaskResult:
    if answer == "killed":
        return progress.ended("blocked", ErrorCode.GATE_BLOCKED, "killed")
    if answer == "no":
        r.out.note("continuation declined; it was not run")
        return progress.ended("blocked", ErrorCode.GATE_BLOCKED, "plan_declined")
    r.out.note("continuation not approved (only the exact go token counts); it was not run")
    return progress.ended("paused", ErrorCode.GATE_PENDING_USER, "plan_unanswered")


def _ready_for(r: Replan, progress: Progress) -> TaskResult | None:
    """The person idle and the task's current window in front before plan n>1 (item 7;
    hunter RP-14: after a `--desktop` start too, once the task left the desktop)."""
    if r.monitor is not None:
        held = wait_for_idle(r.monitor, monotonic=r.deps.monotonic, sleep=r.deps.sleep)
        if held == "takeover":
            return progress.ended("paused", ErrorCode.GATE_PENDING_USER, "takeover")
        if held is not None:  # killed, monitor_lost, ...: blocked under the held token
            return progress.ended("blocked", ErrorCode.GATE_BLOCKED, held)
    if r.deps.session_locked is not None and r.deps.session_locked() is not False:
        return progress.ended("stopped", ErrorCode.SECURE_DESKTOP, "session_locked")
    if r.options.desktop and r.session.hwnd == r.start.hwnd:
        return None  # never the shell's desktop
    try:
        r.deps.acquire_foreground(r.session.hwnd)
    except OSError:
        return progress.ended("stopped", ErrorCode.FOCUS_LOST, "foreground")
    return None


# ---- the real collaborators ----------------------------------------------------------------


def _build_session(bound: Bound) -> SessionLike:
    from steerai.action.execution import UiaBindings  # noqa: PLC0415 - Windows-only COM setup
    from steerai.action.executor import Executor  # noqa: PLC0415
    from steerai.action.launcher import Launcher  # noqa: PLC0415
    from steerai.action.live_reads import UiaLiveReads  # noqa: PLC0415
    from steerai.action.options import UiaOptions  # noqa: PLC0415
    from steerai.action.physical import LiveInput, PhysicalExecutor, tap_refusal  # noqa: PLC0415
    from steerai.orchestrator.gate import Gate  # noqa: PLC0415
    from steerai.orchestrator.loop import Session  # noqa: PLC0415
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415
    from steerai.probes.win_input import acquire_foreground, foreground_hwnd  # noqa: PLC0415
    from steerai.probes.windows import (  # noqa: PLC0415
        list_top_windows,
        session_locked,
        window_identity,
        window_title,
    )
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
        semantic=Executor(blocker=blocker, options=UiaOptions(client)),
        physical=PhysicalExecutor(blocker=blocker),
        gate=Gate(confirmer=bound.confirmer, own_pid=os.getpid(), task_id=bound.task_id),
        ledger=bound.ledger,
        live=UiaLiveReads(client, bindings),
        windows=list_top_windows,
        trace=bound.trace,
        verify_deadline_s=bound.options.verify_deadline_s,
        max_steps=bound.options.max_steps,
        max_risk=bound.options.max_risk,
        monitor=monitor,
        launcher=Launcher(blocker=blocker),
        window_title=window_title,
        identify=window_identity,
        foreground=foreground_hwnd,
        bring_forward=acquire_foreground,
        tap_guard=lambda: tap_refusal(LiveInput(client)),
        locked=session_locked,
        goal_matcher=goal_matcher(model_planned=bound.options.goal is not None),
        address_bar=client.address_bar,
        document_url=client.document_url,
    )


CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")
"""The SDK's environment credentials; a model-planned run is judged whatever it authenticates
with (an on-disk profile too), and a matcher that cannot authenticate asks (`goal_unknown`)."""


def goal_matcher(*, model_planned: bool) -> GoalMatcher | None:
    """Goal-match whenever a model is in the run (5.13 step 2 [v2.27], gate ruling Q4): a
    `--goal` run always has one; a `--plan` replay has one when any credential is set. `off`,
    traced, only for a replay with no credentials of any kind."""
    if not model_planned and not any(os.environ.get(name) for name in CREDENTIAL_VARS):
        return None
    from steerai.goal_match import ClaudeGoalMatcher  # noqa: PLC0415 - the SDK

    return ClaudeGoalMatcher.from_env()


def _kill_blocker(monitor: Any) -> Callable[[], str | None]:
    return lambda: "killed" if monitor.kill_requested() else None


def _screen(hwnd: int, kill: Callable[[], bool] | None) -> ScreenRead:
    """A warm-up observe of the bound window, quiet for PLAN_QUIET_S ([v2.80]) and stopped by
    the kill switch (TK-22, #233): the title and the 6.2 lines for the planner, for the first
    plan and every replan."""
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    model = WorldModel(UiaClient())
    read = warm_up_read(
        lambda: model.observe(hwnd, cap_s=5.0),
        cap_s=DEFAULT_WARMUP_S,
        quiet_s=PLAN_QUIET_S,
        kill=kill,
    )
    return ScreenRead(read.snapshot.title, read.snapshot.lines, capped=read.capped)


def _planner(request: PlanRequest) -> PlanOutcome:
    from steerai.planner.claude_planner import ClaudePlanner  # noqa: PLC0415 - the SDK

    return ClaudePlanner.from_env().plan(request)


def _open_windows() -> list[str]:
    """The titles of registry-app windows outside Steer's own tree (data minimisation: the
    planner sees no other window's title; schema gate non-blocking note)."""
    from steerai.action.launch import APPS  # noqa: PLC0415
    from steerai.probes.windows import (  # noqa: PLC0415
        console_exclusions,
        list_top_windows,
        process_name,
    )

    images = {name for spec in APPS.values() for name in spec.process_names}
    hwnds, pids = console_exclusions()
    return [
        w.title
        for w in list_top_windows()
        if w.hwnd not in hwnds and w.pid not in pids and process_name(w.pid).lower() in images
    ]


def _start_monitor(options: Options) -> Any:
    from steerai.session.monitor import HookMonitor  # noqa: PLC0415 - Windows hooks

    monitor = HookMonitor(kill_key=options.kill_key)
    monitor.start()
    return monitor


def _plan_cache() -> PlanCache | None:
    """Under %LOCALAPPDATA%; off when it is unset, never the working directory (PC-45)."""
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        return None
    from steerai.platform import dpapi  # noqa: PLC0415 - Windows only

    return PlanCache(
        Path(root) / "steer" / "cache" / "plans", protect=dpapi.protect, unprotect=dpapi.unprotect
    )


def _real_deps() -> Deps:
    from steerai.orchestrator.ledger import Ledger  # noqa: PLC0415 - DPAPI, Windows only
    from steerai.probes.win_input import acquire_foreground  # noqa: PLC0415
    from steerai.probes.windows import (  # noqa: PLC0415
        console_exclusions,
        desktop_window,
        list_top_windows,
        session_locked,
        window_identity,
    )

    return Deps(
        list_windows=list_top_windows,
        exclusions=console_exclusions,
        acquire_foreground=acquire_foreground,
        open_ledger=lambda root, task_id: Ledger(root, task_id=task_id, started_at=time.time()),
        build_session=_build_session,
        start_monitor=_start_monitor,
        desktop=desktop_window,
        open_windows=_open_windows,
        screen=_screen,
        planner=_planner,
        session_locked=session_locked,
        plan_cache=_plan_cache(),
        identify=window_identity,
    )


def _closed(ledger: LedgerLike, out: _Out) -> None:
    """Close the ledger without replacing the run's exit code (#61); a lock left behind is said."""
    if ledger.close() == "lock_left":
        out.note(
            "ledger_locked: the task lock was in use by another process and was left; "
            "the next run removes it"
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
