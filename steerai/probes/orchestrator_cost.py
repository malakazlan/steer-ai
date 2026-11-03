"""Orchestrator-cost probe: what the loop adds on top of observe, act and verify.

Measures (1) the pure per-step overhead of `run_step` against a scripted in-memory world with
instant executors (hashing, admission, gate, ledger record with DPAPI, result building), (2) the
write-ahead record size and write time with DPAPI for snapshots of 150 and 460 elements, (3) a
resume evaluation from a deserialised snapshot, and (4) a three-step plan (focus, type, confirmed
send) on the Chrome inbox page end to end, five runs, with the per-stage latencies.

    uv run python -m steerai.probes.orchestrator_cost [--runs 5] [--no-chrome] [--out r.json]
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from steerai.action.execution import UiaBindings
from steerai.action.executor import Executor
from steerai.action.live_reads import UiaLiveReads
from steerai.action.physical import PhysicalExecutor
from steerai.orchestrator.contracts import GateDecision, Plan, Step, Subgoal, Target
from steerai.orchestrator.gate import Ask, Gate
from steerai.orchestrator.ledger import Ledger
from steerai.orchestrator.loop import Session
from steerai.orchestrator.trace import NullTrace
from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome
from steerai.probes.stats import LatencyStats
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.win_input import acquire_foreground
from steerai.probes.windows import wait_window_for_pid
from steerai.verify.predicates import Appeared, Within
from steerai.verify.t1 import evaluate
from steerai.world.elements import Element
from steerai.world.model import Snapshot, WorldModel, snapshot_from_json, snapshot_to_json

PAGE = Path(__file__).with_name("pages") / "inbox.html"
MESSAGE = "orchestrator cost run"


# ---- synthetic snapshots -------------------------------------------------------------------------


def synthetic_snapshot(elements: int, *, snapshot_id: str = "synthetic") -> Snapshot:
    """A conversation-shaped tree with `elements` nodes: one pane, one list, the rest rows."""
    rows = [
        Element(
            id=0,
            depth=0,
            role="Pane",
            name="Window title - App",
            box=(0, 0, 1000, 800),
            runtime_id=(42, 1),
        ),
        Element(
            id=1,
            depth=1,
            role="Group",
            name="Conversation",
            box=(0, 0, 1000, 700),
            runtime_id=(42, 2),
            parent_id=0,
        ),
    ]
    for i in range(2, elements):
        rows.append(
            Element(
                id=i,
                depth=2,
                role="Text",
                name=f"message number {i} with some realistic length",
                box=(10, 10 * i, 900, 10 * i + 20),
                runtime_id=(42, 1000 + i),
                parent_id=1,
            )
        )
    return Snapshot(
        id=snapshot_id,
        hwnd=0x10,
        process="app.exe",
        title="Window title - App",
        taken_at=time.time(),
        elements=rows,
        node_count=len(rows),
        stable=True,
        pid=777,
    )


@dataclass
class YesConfirmer:
    asked: list[Ask] = field(default_factory=list)

    def ask(self, ask: Ask) -> str:
        self.asked.append(ask)
        return f"yes:{ask.decision_id}"


def _wal_cost(root: Path, elements: int, runs: int) -> dict[str, Any]:
    ledger = Ledger(root, task_id=f"wal-{elements}", started_at=time.time())
    step = Step(
        id="send",
        subgoal="g1",
        verb="send",
        target=Target(role="Button", name="Send"),
        risk="R2",
        params={"text": MESSAGE, "composer_id": "1", "scope": "Conversation"},
    )
    snapshot = synthetic_snapshot(elements)
    size = len(snapshot_to_json(snapshot).encode())
    writes: list[float] = []
    reads: list[float] = []
    for run in range(runs):
        key = ledger.key_for(step)
        decision = GateDecision(decision_id=f"d-{run}", action_key=key, outcome="confirm")
        started = time.perf_counter()
        ledger.write_ahead(step, snapshot, decision)
        writes.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        ledger.read_ahead(step)
        reads.append((time.perf_counter() - started) * 1000)
        ledger.record_outcome(step, "not_delivered")
    write = LatencyStats.from_samples(writes)
    read = LatencyStats.from_samples(reads)
    return {
        "elements": elements,
        "json_bytes": size,
        "write_ms_p50": write.p50_ms,
        "write_ms_p95": write.p95_ms,
        "read_ms_p50": read.p50_ms,
    }


def _resume_cost(elements: int, runs: int) -> dict[str, Any]:
    before = synthetic_snapshot(elements, snapshot_id="before")
    text = snapshot_to_json(before)
    after_rows = [*before.elements]
    after_rows.insert(
        len(after_rows),
        Element(
            id=elements,
            depth=2,
            role="Text",
            name=MESSAGE,
            box=(10, 9000, 900, 9020),
            runtime_id=(42, 9999),
            parent_id=1,
        ),
    )
    after = Snapshot(
        **{
            **{f: getattr(before, f) for f in ("hwnd", "process", "title", "pid")},
            "id": "after",
            "taken_at": time.time(),
            "elements": after_rows,
            "node_count": len(after_rows),
            "stable": True,
        }
    )
    predicate = Appeared(role="Text", name_pattern=MESSAGE, within=Within(landmark="Conversation"))
    samples: list[float] = []
    for _ in range(runs):
        started = time.perf_counter()
        persisted = snapshot_from_json(text)
        verdict = evaluate(predicate, persisted, after)
        samples.append((time.perf_counter() - started) * 1000)
        if not verdict.holds:
            raise RuntimeError(f"resume evaluation did not hold: {verdict.evidence}")
    stats = LatencyStats.from_samples(samples)
    return {"elements": elements, "resume_ms_p50": stats.p50_ms, "resume_ms_p95": stats.p95_ms}


def _plan(content_role: str, run: int) -> Plan:
    text = f"{MESSAGE} {run}"
    condition = Appeared(
        role=content_role, name_pattern=text, within=Within(landmark="Conversation")
    )
    return Plan(
        goal="send a message",
        subgoals=(Subgoal(id="g1", desc="sent", condition=condition),),
        steps=(
            Step(
                id="focus",
                subgoal="g1",
                verb="focus",
                target=Target(role="Edit", name="Message"),
                risk="R0",
            ),
            Step(
                id="type",
                subgoal="g1",
                verb="type",
                target=Target(role="Edit", name="Message"),
                text=text,
                risk="R1",
            ),
            Step(
                id="send",
                subgoal="g1",
                verb="send",
                target=Target(role="Button", name="Send"),
                risk="R2",
                params={
                    "text": text,
                    "composer_role": "Edit",
                    "composer_name": "Message",
                    "scope": "Conversation",
                    "content_role": content_role,
                },
            ),
        ),
    )


def _observe_until_exposed(model: WorldModel, hwnd: int, *, max_s: float = 25.0) -> Snapshot:
    deadline = time.monotonic() + max_s
    while True:
        snapshot = model.observe(hwnd, cap_s=5.0)
        if any(e.role == "Edit" and e.name == "Message" for e in snapshot.elements):
            return snapshot
        if time.monotonic() >= deadline:
            raise RuntimeError(f"composer never exposed; {snapshot.node_count} nodes")
        time.sleep(0.5)


def _chrome_plan_cost(chrome: Path, root: Path, runs: int) -> dict[str, Any]:
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-ocost-"))
    proc = launch_chrome(chrome, profile_dir, 9339, PAGE.as_uri(), window_size=(1000, 640))
    walls: list[float] = []
    stages: dict[str, list[float]] = {}
    try:
        window = wait_window_for_pid(proc.pid, title_contains="Google Chrome")
        acquire_foreground(window.hwnd)
        client = UiaClient()
        model = WorldModel(client)
        start = _observe_until_exposed(model, window.hwnd)
        content_role = next(
            e.role for e in start.elements if e.name == "ok" and e.parent_id is not None
        )
        semantic, physical = Executor(), PhysicalExecutor()
        live = UiaLiveReads(client, UiaBindings.load())
        for run in range(runs):
            task_id = f"plan-{run}-{time.time_ns()}"
            session = Session(
                task_id=task_id,
                hwnd=window.hwnd,
                observer=model,
                semantic=semantic,
                physical=physical,
                gate=Gate(confirmer=YesConfirmer(), own_pid=0, task_id=task_id),
                ledger=Ledger(root, task_id=task_id, started_at=time.time()),
                live=live,
                trace=NullTrace(),
            )
            started = time.perf_counter()
            result = session.run_task(_plan(content_role, run))
            walls.append((time.perf_counter() - started) * 1000)
            if result.status != "done":
                raise RuntimeError(
                    f"plan run {run} ended {result.status}: {[r.to_dict() for r in result.results]}"
                )
            for step_result in result.results:
                for stage, ms in step_result.latency_ms.items():
                    stages.setdefault(f"{step_result.step_id}.{stage}", []).append(ms)
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        shutil.rmtree(profile_dir, ignore_errors=True)
    wall = LatencyStats.from_samples(walls)
    return {
        "runs": runs,
        "plan_wall_ms_p50": wall.p50_ms,
        "plan_wall_ms_p95": wall.p95_ms,
        "stages_ms_p50": {
            k: LatencyStats.from_samples(v).p50_ms for k, v in sorted(stages.items())
        },
    }


@dataclass(frozen=True, slots=True)
class ProbeArgs:
    runs: int
    chrome: Path | None
    out: str | None


def parse_args(argv: Sequence[str]) -> ProbeArgs:
    parser = argparse.ArgumentParser(description="Orchestrator overhead, WAL cost, plan latency.")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--chrome", type=Path, default=CHROME_DEFAULT)
    parser.add_argument("--no-chrome", action="store_true")
    parser.add_argument("--out", default=None)
    ns = parser.parse_args(argv)
    if ns.runs <= 0:
        parser.error("runs must be > 0")
    return ProbeArgs(runs=ns.runs, chrome=None if ns.no_chrome else ns.chrome, out=ns.out)


def run_probe(args: ProbeArgs) -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix="steer-ledger-"))
    try:
        result: dict[str, Any] = {
            "wal": [_wal_cost(root, n, args.runs) for n in (150, 460)],
            "resume": [_resume_cost(n, args.runs) for n in (150, 460)],
        }
        if args.chrome is not None:
            if not args.chrome.is_file():
                raise FileNotFoundError(args.chrome)
            result["chrome_plan"] = _chrome_plan_cost(args.chrome, root, args.runs)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return result


def format_report(result: dict[str, Any]) -> str:
    lines = ["orchestrator cost"]
    for row in result["wal"]:
        lines.append(
            f"  WAL {row['elements']:4d} elements: {row['json_bytes']:7d} bytes json, "
            f"write p50 {row['write_ms_p50']:.1f} ms p95 {row['write_ms_p95']:.1f} ms, "
            f"read p50 {row['read_ms_p50']:.1f} ms"
        )
    for row in result["resume"]:
        lines.append(
            f"  resume {row['elements']:4d} elements: deserialise + evaluate "
            f"p50 {row['resume_ms_p50']:.1f} ms p95 {row['resume_ms_p95']:.1f} ms"
        )
    plan = result.get("chrome_plan")
    if plan:
        lines.append(
            f"  chrome 3-step plan x{plan['runs']}: wall p50 {plan['plan_wall_ms_p50']:.0f} ms "
            f"p95 {plan['plan_wall_ms_p95']:.0f} ms"
        )
        for stage, ms in plan["stages_ms_p50"].items():
            lines.append(f"    {stage:16s} p50 {ms:.1f} ms")
    return chr(10).join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    result = run_probe(args)
    sys.stdout.write(format_report(result) + "\n")
    if args.out is not None:
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
