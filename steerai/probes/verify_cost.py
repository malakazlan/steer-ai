"""Verify-cost probe: what a T1 `verify` costs on the pass path and on the timeout path.

Notepad: `set_value` on the document, then `ValueEquals` (pass) and `ValueEquals` of a text the
document never gets (timeout). Chrome inbox page: physical type + semantic Send, then `Appeared`
of the sent text in the conversation (pass) and `Appeared` of a text never sent (timeout). Each
case runs `runs` times; wall time, polls and the observe cost are recorded.

    uv run python -m steerai.probes.verify_cost [--runs 5] [--no-chrome] [--chrome path]
        [--out r.json]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from steerai.action.execution import UiaBindings
from steerai.action.executor import Action, Executor
from steerai.action.live_reads import UiaLiveReads
from steerai.action.physical import PhysicalAction, PhysicalExecutor
from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome
from steerai.probes.stats import LatencyStats
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.win_input import acquire_foreground
from steerai.probes.windows import wait_window_for_pid
from steerai.verify.predicates import Appeared, Predicate, ValueEquals, Within
from steerai.verify.t1 import Verdict, verify
from steerai.world.model import Snapshot, WorldModel

PAGE = Path(__file__).with_name("pages") / "inbox.html"


@dataclass(frozen=True, slots=True)
class Trial:
    target: str
    case: str
    holds: bool
    code: str | None
    polls: int
    wall_ms: float
    observe_ms: float
    """One `observe(stable_window=1)` of the same window, measured right before the verify."""


@dataclass(frozen=True, slots=True)
class _Bench:
    """One window under test: the model that observes it and the live reads for it."""

    target: str
    model: WorldModel
    hwnd: int
    live: UiaLiveReads

    def run(self, case: str, predicate: Predicate, before: Snapshot) -> Trial:
        started = time.perf_counter()
        after = self.model.observe(self.hwnd, cap_s=5.0, stable_window=1)
        observe_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        verdict: Verdict = verify(
            predicate,
            before,
            after,
            lambda **kw: self.model.observe(self.hwnd, cap_s=5.0, **kw),
            live=self.live,
        )
        wall_ms = (time.perf_counter() - started) * 1000
        return Trial(
            target=self.target,
            case=case,
            holds=verdict.holds,
            code=verdict.code.value if verdict.code else None,
            polls=verdict.polls,
            wall_ms=wall_ms,
            observe_ms=observe_ms,
        )


def _notepad_trials(model: WorldModel, live: UiaLiveReads, runs: int) -> list[Trial]:
    trials: list[Trial] = []
    for run in range(runs):
        proc = subprocess.Popen(["notepad.exe"])  # noqa: S607 - fixed, well-known executable
        try:
            window = wait_window_for_pid(proc.pid)
            time.sleep(0.5)
            acquire_foreground(window.hwnd)
            before = model.observe(window.hwnd, cap_s=5.0)
            target = next(e for e in before.elements if e.role in ("Document", "Edit"))
            text = f"steer verify run {run}"
            result = Executor().execute(
                Action(verb="set_value", element_id=target.id, text=text), before
            )
            if not result.ok:
                raise RuntimeError(f"set_value failed: {result.to_dict()}")
            bench = _Bench("notepad", model, window.hwnd, live)
            trials.append(bench.run("pass", ValueEquals(target_id=target.id, text=text), before))
            trials.append(
                bench.run("timeout", ValueEquals(target_id=target.id, text="never typed"), before)
            )
        finally:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
    return trials


def _observe_until_exposed(model: WorldModel, hwnd: int, *, max_s: float = 25.0) -> Snapshot:
    """Chromium's first-contact tree is thin and reads as stable (probe 5 ledger: cold settle
    10-25 s); observe until the composer is exposed or `max_s` elapse."""
    deadline = time.monotonic() + max_s
    while True:
        snapshot = model.observe(hwnd, cap_s=5.0)
        if any(e.role == "Edit" and e.name == "Message" for e in snapshot.elements):
            return snapshot
        if time.monotonic() >= deadline:
            raise RuntimeError(f"composer never exposed; {snapshot.node_count} nodes")
        time.sleep(0.5)


def _chrome_trials(
    chrome: Path, model: WorldModel, live: UiaLiveReads, runs: int, *, noop: bool = False
) -> list[Trial]:
    """Pass and timeout paths on the inbox page; with `noop` the Send button does nothing, so a
    pass is a false success (the probe records it as the `noop` case)."""
    trials: list[Trial] = []
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-vcost-"))
    url = PAGE.as_uri() + ("?noop=1" if noop else "")
    target = "chrome-noop" if noop else "chrome"
    proc = launch_chrome(chrome, profile_dir, 9337, url, window_size=(1000, 640))
    try:
        window = wait_window_for_pid(proc.pid, title_contains="Google Chrome")
        acquire_foreground(window.hwnd)
        physical = PhysicalExecutor()
        for run in range(runs):
            before = _observe_until_exposed(model, window.hwnd)
            composer = next(e for e in before.elements if e.role == "Edit" and e.name == "Message")
            send = next(e for e in before.elements if e.role == "Button" and e.name == "Send")
            role = (
                next(e.role for e in before.elements if e.parent_id is not None and e.name == "ok")
                if any(e.name == "ok" for e in before.elements)
                else "Text"
            )
            text = f"verify cost run {run}"
            # The no-op page never clears the composer; typing refuses a non-empty field.
            cleared = Executor().execute(
                Action(verb="set_value", element_id=composer.id, text=""), before
            )
            if not cleared.ok:
                raise RuntimeError(f"clear failed: {cleared.to_dict()}")
            clicked = physical.execute(PhysicalAction(verb="click", element_id=composer.id), before)
            typed = physical.execute(
                PhysicalAction(verb="type", element_id=composer.id, text=text), before
            )
            pressed = Executor().execute(Action(verb="click", element_id=send.id), before)
            if not (typed.ok and pressed.delivery.value.startswith("delivered")):
                raise RuntimeError(
                    f"send failed: {clicked.to_dict()} {typed.to_dict()} {pressed.to_dict()}"
                )
            scope = Within(landmark="Conversation")
            bench = _Bench(target, model, window.hwnd, live)
            trials.append(
                bench.run(
                    "pass",
                    Appeared(
                        role=role, name_pattern=text, within=scope, excluding_anchor=composer.id
                    ),
                    before,
                )
            )
            if noop:
                continue
            trials.append(
                bench.run(
                    "timeout", Appeared(role=role, name_pattern="never sent", within=scope), before
                )
            )
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        shutil.rmtree(profile_dir, ignore_errors=True)
    return trials


def summarise(trials: Sequence[Trial]) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for trial in trials:
        rows = out.setdefault(trial.target, {}).setdefault(
            trial.case, {"walls": [], "observes": [], "polls": [], "outcomes": []}
        )
        rows["walls"].append(trial.wall_ms)
        rows["observes"].append(trial.observe_ms)
        rows["polls"].append(trial.polls)
        rows["outcomes"].append((trial.holds, trial.code))
    summary: dict[str, dict[str, dict[str, Any]]] = {}
    for target, cases in out.items():
        summary[target] = {}
        for case, rows in cases.items():
            walls = LatencyStats.from_samples(rows["walls"])
            observes = LatencyStats.from_samples(rows["observes"])
            summary[target][case] = {
                "runs": len(rows["walls"]),
                "wall_ms_p50": walls.p50_ms,
                "wall_ms_p95": walls.p95_ms,
                "observe_ms_p50": observes.p50_ms,
                "polls_max": max(rows["polls"]),
                "outcomes": sorted({f"{h}:{c}" for h, c in rows["outcomes"]}),
            }
    return summary


@dataclass(frozen=True, slots=True)
class ProbeArgs:
    runs: int
    chrome: Path | None
    out: str | None


def parse_args(argv: Sequence[str]) -> ProbeArgs:
    parser = argparse.ArgumentParser(description="T1 verify cost on the pass and timeout paths.")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--chrome", type=Path, default=CHROME_DEFAULT)
    parser.add_argument("--no-chrome", action="store_true")
    parser.add_argument("--out", default=None)
    ns = parser.parse_args(argv)
    if ns.runs <= 0:
        parser.error("runs must be > 0")
    return ProbeArgs(runs=ns.runs, chrome=None if ns.no_chrome else ns.chrome, out=ns.out)


def run_probe(args: ProbeArgs) -> dict[str, Any]:
    client = UiaClient()
    model = WorldModel(client)
    live = UiaLiveReads(client, UiaBindings.load())
    trials = _notepad_trials(model, live, args.runs)
    if args.chrome is not None:
        if not args.chrome.is_file():
            raise FileNotFoundError(args.chrome)
        trials += _chrome_trials(args.chrome, model, live, args.runs)
        trials += _chrome_trials(args.chrome, model, live, args.runs, noop=True)
    return {
        "runs": args.runs,
        "trials": [asdict(t) for t in trials],
        "summary": summarise(trials),
    }


def format_report(result: dict[str, Any]) -> str:
    lines = [f"verify cost, {result['runs']} runs per case"]
    for target, cases in result["summary"].items():
        for case, row in cases.items():
            lines.append(
                f"  {target:8s} {case:8s} wall p50 {row['wall_ms_p50']:.0f} ms p95 "
                f"{row['wall_ms_p95']:.0f} ms  observe p50 {row['observe_ms_p50']:.0f} ms  "
                f"polls max {row['polls_max']}  outcomes {row['outcomes']}"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    result = run_probe(args)
    sys.stdout.write(format_report(result) + "\n")
    if args.out is not None:
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
