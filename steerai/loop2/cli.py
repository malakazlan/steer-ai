"""`python -m steerai.run --loop v2`: the v2 loop switched on (slice 7b, [V2.53]).

`--loop v2 --goal TEXT (--hwnd N | --window TITLE)`, with the budgets and the price file. It
refuses to start unless step 3 is ready; it binds exactly one window through v1's matcher (the
console excluded, candidates listed otherwise); it reads the price file (`loop2.price`) and the
budgets, none unlimited; it prints the run header with every limit in force; it runs the task on
its own loop thread, where COM is released on every path; and it writes one JSON line on stdout
with the use against each limit, the limit that paused the task, the listing and focus counts
and the world's report. No `--plan`, no `--desktop` (step 7) and no debug trace flag
(invariant 4: only a probe that builds the config itself reaches the debug level).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import traceback
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Final, TextIO

from steerai.errors import ErrorCode, SteerError
from steerai.loop2 import ready as ready_step
from steerai.loop2.decide_port import TaskGenerations
from steerai.loop2.live_world import LiveConfig, LiveWorld, Platform, run_on_loop_thread
from steerai.loop2.loop import TaskOutcome
from steerai.loop2.price import STALE_DAYS, PriceFile, load_price
from steerai.loop2.runner import run_task
from steerai.loop2.task import Budgets, Envelope, Task
from steerai.orchestrator.console import escape_text
from steerai.orchestrator.trace import JsonLinesTrace, NullTrace, Trace
from steerai.planner.claude_planner import DEFAULT_MODEL
from steerai.probes.windows import WindowInfo
from steerai.run import (
    EXIT_BLOCKED,
    EXIT_DONE,
    EXIT_ERROR,
    EXIT_PAUSED,
    EXIT_STOPPED,
    EXIT_USAGE,
    RunnerError,
    UsageError,
    _hwnd,
    _usable_goal,
    resolve_window,
)
from steerai.session.monitor import DEFAULT_KILL_KEY, parse_kill_key
from steerai.world.reads import ReadCost

DEFAULT_BUDGETS: Final = (20, 40, 400_000, 300.0)
"""[V2.53] (3), provisional: model calls, actions, tokens, wall seconds."""
BUDGET_TOKENS: Final = frozenset(
    {"max_actions", "max_model_calls", "max_tokens", "max_cost", "max_wall"}
)
EXIT_FOR: Final[Mapping[str, int]] = {
    "done": EXIT_DONE,
    "paused": EXIT_PAUSED,
    "asked": EXIT_PAUSED,
    "blocked": EXIT_BLOCKED,
    "stopped": EXIT_STOPPED,
    "cannot": EXIT_STOPPED,
}
"""The task's status as v1's exit codes; `asked` waits for the person as a pause does."""


@dataclass(frozen=True)
class V2Options:
    goal: str
    window: str | None
    hwnd: int | None
    task_id: str
    kill_key: str
    trace: Path | None
    price_file: Path
    max_model_calls: int
    max_actions: int
    max_tokens: int
    max_wall_s: float
    hook_helper: bool = False
    """[V2.60]: the hooks in their own helper process (`--hook-helper`); off by default."""


@dataclass(frozen=True)
class V2Deps:
    """The run's collaborators, injected so the command line runs pure in tests."""

    list_windows: Callable[[], Sequence[WindowInfo]]
    exclusions: Callable[[], tuple[set[int], set[int]]]
    """(hwnds, pids) never to bind: this console and its process tree (v1's RN-15)."""
    platform: Callable[[], Platform]
    today: Callable[[], date]
    image_of: Callable[[int], str]
    """A process's image name, lower case (v1's `process_name`): bound with the listed pid."""
    input_methods: Callable[[], Sequence[int]]
    """[V2.63] Q3: the installed input locales (HKLs); OSError when unread."""
    ime_record: Callable[[], Mapping[str, Any] | None]
    """[V2.63] Q3: the open-IME probe's recorded pass on this machine (`ime_gate`)."""
    ready: Callable[[int], bool] = ready_step
    clock: Callable[[], float] = field(default=time.monotonic)
    pause: Callable[[float], None] = field(default=time.sleep)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # Any: never returns; a usage error exits 1
        raise UsageError(message)


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a whole number") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be above zero: no budget is unlimited")
    return value


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a number of seconds") from exc
    if not (math.isfinite(value) and value > 0):
        raise argparse.ArgumentTypeError("must be finite and above zero")
    return value


def parse_v2(argv: Sequence[str]) -> V2Options:
    parser = _Parser(prog="python -m steerai.run --loop v2", allow_abbrev=False)
    parser.add_argument("--loop", choices=["v2"], required=True)
    parser.add_argument("--goal", required=True, help="what the person asks for")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--window", help="title substring of the one window to act on")
    which.add_argument("--hwnd", type=_hwnd, help="the window handle instead of a title")
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--kill-key", default=DEFAULT_KILL_KEY)
    parser.add_argument("--trace", type=Path, default=None, help="the clear JSON-lines trace")
    parser.add_argument("--price-file", type=Path, default=None)
    calls, actions, tokens, wall = DEFAULT_BUDGETS
    parser.add_argument("--max-model-calls", type=_positive_int, default=calls)
    parser.add_argument("--max-actions", type=_positive_int, default=actions)
    parser.add_argument("--max-tokens", type=_positive_int, default=tokens)
    parser.add_argument("--max-wall-s", type=_positive_seconds, default=wall)
    parser.add_argument(
        "--hook-helper",
        action="store_true",
        help="run the hooks and the kill key in a helper process ([V2.60])",
    )
    try:
        args = parser.parse_args(list(argv))
    except argparse.ArgumentError as exc:
        raise UsageError(str(exc)) from exc
    try:
        parse_kill_key(args.kill_key)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    return V2Options(
        goal=_usable_goal(args.goal),
        window=args.window,
        hwnd=args.hwnd,
        task_id=args.task_id or f"task-{uuid.uuid4().hex[:12]}",
        kill_key=str(args.kill_key),
        trace=args.trace,
        price_file=args.price_file if args.price_file is not None else _default_price_file(),
        max_model_calls=args.max_model_calls,
        max_actions=args.max_actions,
        max_tokens=args.max_tokens,
        max_wall_s=args.max_wall_s,
        hook_helper=bool(args.hook_helper),
    )


def _default_price_file() -> Path:
    """`%LOCALAPPDATA%/steer/price.json`; with LOCALAPPDATA unset, a refusal, never the working
    directory (#361 H11)."""
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        raise UsageError("LOCALAPPDATA is unset: give the price file with --price-file")
    return Path(root) / "steer" / "price.json"


def bind_window(
    windows: Sequence[WindowInfo],
    exclusions: tuple[set[int], set[int]],
    *,
    hwnd: int | None,
    title: str | None,
) -> WindowInfo:
    """[V2.53] (1): v1's matcher (`run._pick_window` without `--desktop`): exactly one window,
    never this console, never minimized or on an unreadable desktop. A test holds the two equal."""
    excluded_hwnds, excluded_pids = exclusions
    if hwnd is not None:
        listed = [w for w in windows if w.hwnd == hwnd]
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
    if title is None:
        raise UsageError("--window or --hwnd is required")
    return resolve_window(
        windows, title, excluded_hwnds=excluded_hwnds, excluded_pids=excluded_pids
    )


def real_deps() -> V2Deps:
    from steerai.loop2.ime_gate import read_record, steer_dir  # noqa: PLC0415
    from steerai.loop2.live_world import WindowsPlatform  # noqa: PLC0415 - Windows only
    from steerai.probes.ime_probe import installed_layouts  # noqa: PLC0415 - Windows only
    from steerai.probes.windows import (  # noqa: PLC0415 - Windows only
        console_exclusions,
        list_top_windows,
        process_name,
    )

    return V2Deps(
        list_windows=list_top_windows,
        exclusions=console_exclusions,
        platform=WindowsPlatform,
        today=date.today,
        image_of=lambda pid: process_name(pid).lower(),
        input_methods=installed_layouts,
        ime_record=lambda: read_record(steer_dir()),
    )


def main(  # noqa: PLR0911 - one return per start refusal, in order
    argv: Sequence[str],
    *,
    deps: V2Deps | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = _Out(sys.stdout if stdout is None else stdout, sys.stderr if stderr is None else stderr)
    try:
        options = parse_v2(argv)
    except UsageError as exc:
        out.note(f"usage error: {exc}")
        return out.error(None, "usage", task_id=None)
    deps = real_deps() if deps is None else deps
    if not deps.ready(3):
        out.note("error: loop v2 is not built yet: step 3")
        return out.error(None, "loop_v2_not_ready", task_id=options.task_id)
    try:
        price = load_price(options.price_file, model=DEFAULT_MODEL, today=deps.today())
    except SteerError as exc:
        return out.error(exc.code, exc.message, task_id=options.task_id)
    refused = _ime_refused(deps)
    if refused is not None:
        out.note(
            "error: a Chinese, Japanese or Korean IME is installed and the open-IME probe has no "
            "recorded pass on this machine: run python -m steerai.probes.ime_probe first"
        )
        return out.error(ErrorCode.GATE_BLOCKED, refused, task_id=options.task_id)
    try:
        window = bind_window(
            deps.list_windows(), deps.exclusions(), hwnd=options.hwnd, title=options.window
        )
    except RunnerError as exc:
        out.candidates(exc.candidates)
        return out.error(exc.code, exc.detail, task_id=options.task_id)
    except UsageError as exc:
        out.note(f"usage error: {exc}")
        return out.error(None, "usage", task_id=options.task_id)
    return _run(options, deps, out, price, window)


def _ime_refused(deps: V2Deps) -> str | None:
    """[V2.63] Q3, before anything is built: an installed input method list that cannot be read
    proves nothing either, so it refuses the same way."""
    from steerai.loop2.ime_gate import UNPROBED, ime_refusal  # noqa: PLC0415 - at start only

    try:
        installed = deps.input_methods()
    except OSError:
        return UNPROBED
    return ime_refusal(installed, deps.ime_record)


def _run(options: V2Options, deps: V2Deps, out: _Out, price: PriceFile, window: WindowInfo) -> int:
    task = Task(
        id=options.task_id,
        goal=options.goal,
        subgoals=(),
        envelope=Envelope(grants=(), mode="bootstrap"),
        budgets=Budgets(
            max_actions=options.max_actions,
            max_model_calls=options.max_model_calls,
            max_tokens=options.max_tokens,
            max_cost_usd=price.max_cost_usd,
            max_wall_s=options.max_wall_s,
            price=price.price,
        ),
        history=[],
    )
    trace: Trace = NullTrace() if options.trace is None else JsonLinesTrace(options.trace)
    config = LiveConfig(
        task_id=options.task_id,
        hwnd=window.hwnd,
        kill_key=options.kill_key,
        trace_level="redacted",
        trace=trace,
        generations=TaskGenerations(),
        clock=deps.clock,
        pause=deps.pause,
        bound_pid=window.pid,
        bound_image=deps.image_of(window.pid),
        hook_helper=options.hook_helper,
    )
    platform = deps.platform()

    done: dict[str, TaskOutcome] = {}
    started: list[bool] = []

    def work() -> tuple[TaskOutcome, dict[str, int | str | None]]:
        with LiveWorld(config, platform) as world:
            world.bring_to_front()  # [V2.62]: a refusal here is a start refusal, its own reason
            started.append(True)
            limits = _limits_header(task.budgets, price)
            out.note("header: " + json.dumps({**world.header(task), **limits}, sort_keys=True))
            if price.stale:
                out.note(
                    f"warning: the price was read on {price.price.read_on.isoformat()}, more "
                    f"than {STALE_DAYS} days ago; re-read it"
                )
            ports = world.ports(task, header=limits)  # the trace's one header record
            outcome = run_task(task, ports)
            done["outcome"] = outcome
            del ports  # rev 4.0c: nothing holding COM outlives the world on this thread
            return outcome, world.report()

    interrupts: list[bool] = []
    try:
        outcome, report = run_on_loop_thread(
            work, release=platform.release_com, interrupted=lambda: interrupts.append(True)
        )
    except SteerError as exc:
        if not started:  # the world refused to start: the session, the monitor, the window
            out.emit({"status": "blocked", "code": exc.code.value, "detail": exc.message}, options)
            return EXIT_BLOCKED
        # #361 H5: after the start, the run's own shape, with what was counted
        kept = done.get("outcome")
        result = _unfinished(task.budgets) if kept is None else _result(kept, task.budgets, {})
        result.update(
            status="stopped", reason=f"port_{exc.code.value}"[:64], error=exc.message, world=None
        )
        out.emit(result, options)
        return EXIT_STOPPED
    except Exception:  # noqa: BLE001 - as v1's run(): the last line of defence, one exit code
        for line in traceback.format_exc().splitlines():
            out.note(line)  # escaped: exception text can quote titles or page text
        out.emit({"status": "error", "code": None, "detail": "internal_error"}, options)
        return EXIT_ERROR
    result = _result(outcome, task.budgets, report)
    if interrupts:  # #361 P2: Ctrl+C during the join; the outcome is still the task's own
        out.note("interrupted: Ctrl+C arrived while the task ran; the kill hot key is the stop")
        out.emit({**result, "interrupted": True}, options)
        return EXIT_STOPPED
    out.emit(result, options)
    return EXIT_FOR[outcome.status]


def _unfinished(budgets: Budgets) -> dict[str, Any]:  # Any: the final JSON
    """#361 H5: the run's shape when the task ended with no outcome: nothing was counted, so
    every use is null and said, never 0."""
    return {
        "paused_by": None,
        "monitor_lost": False,
        "used": dict.fromkeys(_limits(budgets)),
        "cache_tokens": dict.fromkeys(("write_5m", "write_1h", "read")),  # unknown, said
        "tokens_incomplete": True,
        "cost_incomplete": True,
        "wall_incomplete": True,
        "limits": _limits(budgets),
        "listings": [],
        "focus": None,
        **dict.fromkeys(RUN_FACTS),  # unknown, said
    }


RUN_FACTS: Final = (
    "read_cost",
    "strikes",
    "stale_observations",
    "unread_automation_ids",
    "url_reads",
    "refusals",
    "counts",
)
"""Stage 2 plan §6 (3), [V2.63]: the run facts the final line carries beside the use and the
listings; `counts` holds the counts that were on the outcome only."""


def read_cost_summary(costs: Sequence[ReadCost]) -> dict[str, float | int | None]:
    """The full-read cost per observation (each observation's milliseconds per full read, from
    the reads that produced it), summarised: never the list, so the line stays small."""
    per = sorted(c.full_ms / c.full_reads for c in costs if c.full_reads > 0)

    def at(q: float) -> float | None:
        return round(per[min(len(per) - 1, math.ceil(q * len(per)) - 1)], 1) if per else None

    return {
        "observations": len(per),
        "full_reads": sum(c.full_reads for c in costs),
        "p50_ms": at(0.5),
        "p95_ms": at(0.95),
        "max_ms": round(per[-1], 1) if per else None,
    }


def _counted(items: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return out


def _limits(budgets: Budgets) -> dict[str, int | float]:
    return {
        "max_model_calls": budgets.max_model_calls,
        "max_actions": budgets.max_actions,
        "max_tokens": budgets.max_tokens,
        "max_wall_s": budgets.max_wall_s,
        "max_cost_usd": budgets.max_cost_usd,
    }


def _limits_header(budgets: Budgets, price: PriceFile) -> dict[str, str]:
    """[V2.53] (3): every limit in force, and whether the price's read date is old."""
    return {
        "max_model_calls": str(budgets.max_model_calls),
        "max_actions": str(budgets.max_actions),
        "max_tokens": str(budgets.max_tokens),
        "max_wall_s": str(budgets.max_wall_s),
        "max_cost_usd": str(budgets.max_cost_usd),
        "price_stale": "true" if price.stale else "false",
        "price_stale_after_days": str(STALE_DAYS),
    }


def _result(
    outcome: TaskOutcome, budgets: Budgets, report: Mapping[str, int | str | None]
) -> dict[str, Any]:  # Any: the final JSON
    """[V2.53] (3): use against each limit, the limit that paused the task, the listing and
    focus counts, and the world's report. An unknown token count is `null` and said, never NaN."""
    tokens_known = math.isfinite(outcome.tokens)
    cost = outcome.cost_usd
    cost_known = cost is not None and math.isfinite(cost)  # #361 P1: NaN once usage is unknown
    wall_known = math.isfinite(outcome.wall_s)
    reason = outcome.reason
    return {
        "status": outcome.status,
        "reason": reason,
        "paused_by": reason if outcome.status == "paused" and reason in BUDGET_TOKENS else None,
        "used": {
            "max_model_calls": outcome.model_calls,
            "max_actions": len(outcome.dispatched),
            "max_tokens": outcome.tokens if tokens_known else None,
            "max_wall_s": round(outcome.wall_s, 3) if wall_known else None,
            "max_cost_usd": cost if cost_known else None,
        },
        "monitor_lost": reason == "monitor_lost" or bool(report.get("monitor_lost")),
        "rejections": list(outcome.rejections),  # closed tokens: why a turn was rejected
        "exact_counts": outcome.exact_counts,  # [V2.61]: turns near the ceiling, counted exactly
        "estimates_low": outcome.estimates_low,  # [V2.61]: turns whose byte bound was low
        "cache_tokens": {  # [V2.45]: stage 1 asserts each is zero (caching is off)
            "write_5m": outcome.usage.cache_write_5m,
            "write_1h": outcome.usage.cache_write_1h,
            "read": outcome.usage.cache_read,
        },
        "read_cost": read_cost_summary(outcome.read_costs),
        "strikes": {
            "count": len(outcome.strikes),
            "by_reason": _counted(r for reasons in outcome.strikes for r in reasons),
        },
        "stale_observations": outcome.stale_observations,
        "unread_automation_ids": outcome.unread_automation_ids,
        "url_reads": dict(outcome.url_reads),
        "refusals": dict(outcome.refusals),  # [V2.46]: by closed token
        "counts": {  # [V2.63]: every count the rulings asked for, collected before stage 2
            "second_toggles": outcome.second_toggles,
            "second_toggles_refused": outcome.second_toggles_refused,
            "toggles_settled_on_reread": outcome.toggles_settled_on_reread,
            "fields_not_empty": outcome.fields_not_empty,
            "not_own_text": outcome.not_own_text,
            "target_changes": outcome.target_changes,
            "focus_losses": outcome.focus_losses,
            "history_dropped": outcome.history_dropped,
            "submit_parents": dict(outcome.submit_parents),
        },
        "watch_deadlines": {  # [V2.48]: per class, watches and those ended at the deadline
            kind: {"watches": watches, "deadline": hits}
            for kind, watches, hits in outcome.watch_deadlines
        },
        "tokens_incomplete": not tokens_known,
        "cost_incomplete": not cost_known,
        "wall_incomplete": not wall_known,
        "limits": _limits(budgets),
        "listings": [
            {
                "eligible": t.eligible,
                "shown": t.shown,
                "cut": t.cut,
                "keep_at_ceiling": t.keep_at_ceiling,
                "goal_at_subcap": t.goal_at_subcap,
                "prompt_cut": t.prompt_cut,
                "prompt_tokens": t.prompt_tokens,
                "root_depth": t.root_depth,
                "prompt_bytes": t.prompt_bytes,
            }
            for t in outcome.listings
        ],
        "focus": {
            "observations_focus_unread": outcome.observations_focus_unread,
            "observations_focus_elsewhere": outcome.observations_focus_elsewhere,
            "type_unfocused": outcome.type_unfocused,
            "focus_moved_after_press": outcome.focus_moved_after_press,
        },
        "world": dict(report),
    }


class _Out:
    def __init__(self, stdout: TextIO, stderr: TextIO) -> None:
        self._stdout = stdout
        self._stderr = stderr

    def note(self, text: str) -> None:
        self._stderr.write(escape_text(text) + "\n")

    def candidates(self, windows: Sequence[WindowInfo]) -> None:
        """As v1 lists them: other processes' titles, escaped (RN-05)."""
        if not windows:
            self.note("no visible window outside this console")
        for w in windows:
            flag = " (minimized)" if w.minimized else ""
            self.note(f"  hwnd 0x{w.hwnd:x} pid {w.pid}{flag}: {w.title}")

    def emit(self, payload: Mapping[str, Any], options: V2Options) -> None:  # Any: JSON values
        line = json.dumps({"task_id": options.task_id, **payload}, sort_keys=True, allow_nan=False)
        self._stdout.write(line + "\n")

    def error(self, code: ErrorCode | None, detail: str, *, task_id: str | None) -> int:
        payload = {
            "task_id": task_id,
            "status": "error",
            "code": None if code is None else code.value,
            "detail": detail,
        }
        self._stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return EXIT_USAGE
