"""Tag-proof hook probe ([V2.81] laptop item): the shipped v2 monitors, probe by probe.

`HookMonitor(per_hook=True)` (hook mode) and the hook helper's `HelperMonitor` (helper mode) are
driven through their own `probe_now`, `probe_seen` and `lost_hooks`; each probe is classified per
hook as proven (its own tag echoed within the derived deadline), late (echoed after it) or lost
(never echoed). Also counted per probe: probe-shaped injected events the classifier took for
foreign (`injected_other` rose while only Steer injected) and a merged move (the mouse hook
unproven while physical input was counted in the window). Only counts and times are kept: no key
code, no tag, nothing the person types.

    uv run python -m steerai.loop2.hook_tags_probe --i-am-present --out DIR [--probes 500]
        [--interval-ms 150] [--cases hook-idle helper-idle hook-moving helper-moving]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

HOOKS = ("keyboard", "mouse")
CASES = ("hook-idle", "helper-idle", "hook-moving", "helper-moving")
PROVEN, LATE, LOST = "proven", "late", "lost"
POLL_S = 0.001
GRACE_S = 0.5
"""How long past the deadline a probe waits for a late echo before it is lost (unmeasured)."""
SETTLE_S = 1.0
WARMUP = 2
MOVE_MARGIN_S = 10


class Reads(Protocol):
    """What the probe reads of a monitor, on one clock: each hook's last proof (the tag of the
    last probe it saw; never kept, only compared with the value before the probe) and the time
    that proof arrived, and the classifier's counters."""

    def now(self) -> float: ...
    def proof(self, hook: str) -> int: ...
    def proof_at(self, hook: str) -> float: ...
    def count(self, kind: str) -> int: ...


class ProbedMonitor(Protocol):
    def probe_now(self) -> None: ...
    def probe_seen(self) -> bool: ...
    def lost_hooks(self) -> tuple[str, ...]: ...
    def kill_requested(self) -> bool: ...


Row = dict[str, Any]  # Any: JSON values


class Killed(Exception):  # noqa: N818 - the probe's own stop, said as its reason
    """The monitor's own kill chord was pressed: the run stops at once."""


def hook_outcome(
    *, sent: float, proof_at: float | None, deadline_s: float
) -> tuple[str, float | None]:
    """One hook of one probe: (outcome, echo latency in ms); no latency when never echoed."""
    if proof_at is None:
        return LOST, None
    latency = proof_at - sent
    return (PROVEN if latency <= deadline_s else LATE), latency * 1000


def probe_outcome(hooks: dict[str, str]) -> str:
    values = set(hooks.values())
    if LOST in values:
        return LOST
    return LATE if LATE in values else PROVEN


def probe_row(  # noqa: PLR0913 - one probe's facts, all keyword
    *,
    case: str,
    index: int,
    sent_s: float,
    deadline_s: float,
    proofs: dict[str, float | None],
    sent: float,
    monitor_lost: Sequence[str],
    foreign: int,
    physical: int,
) -> Row:
    per: dict[str, Row] = {}
    for hook in HOOKS:
        outcome, latency = hook_outcome(sent=sent, proof_at=proofs[hook], deadline_s=deadline_s)
        per[hook] = {"outcome": outcome, "latency_ms": latency}
    outcomes = {hook: str(per[hook]["outcome"]) for hook in HOOKS}
    return {
        "case": case,
        "probe": index,
        "sent_s": round(sent_s, 6),
        "outcome": probe_outcome(outcomes),
        "hooks": per,
        "monitor_lost": list(monitor_lost),
        "own_classed_foreign": foreign,
        "physical_events": physical,
        "merged_move_suspect": outcomes["mouse"] != PROVEN and physical > 0,
    }


def run_case(  # noqa: PLR0913 - the probe's seams, all keyword: fakes in tests
    case: str,
    monitor: ProbedMonitor,
    reads: Reads,
    *,
    probes: int,
    interval_s: float,
    deadline_s: float,
    sleep: Callable[[float], None] = time.sleep,
    grace_s: float = GRACE_S,
    warmup: int = WARMUP,
    emit: Callable[[Row], None],
) -> list[Row]:
    """`warmup` unrecorded probes, then `probes` recorded ones spaced `interval_s` apart. The
    monitor's own kill chord is checked before every probe."""
    rows: list[Row] = []
    origin = reads.now()
    for index in range(-warmup, probes):
        if monitor.kill_requested():
            raise Killed
        before = {hook: reads.proof(hook) for hook in HOOKS}
        foreign0, physical0 = reads.count("injected_other"), reads.count("user")
        sent = reads.now()
        monitor.probe_now()
        end = sent + deadline_s + grace_s
        proofs: dict[str, float | None] = dict.fromkeys(HOOKS)
        while reads.now() <= end:
            for hook in HOOKS:
                if proofs[hook] is None and reads.proof(hook) != before[hook]:
                    proofs[hook] = reads.proof_at(hook)
            if all(value is not None for value in proofs.values()):
                break
            sleep(POLL_S)
        lost = monitor.lost_hooks() if reads.now() > sent + deadline_s else ()
        sleep(max(0.0, interval_s - (reads.now() - sent)))
        if index < 0:
            continue
        row = probe_row(
            case=case,
            index=index,
            sent_s=sent - origin,
            deadline_s=deadline_s,
            proofs=proofs,
            sent=sent,
            monitor_lost=lost,
            foreign=reads.count("injected_other") - foreign0,
            physical=reads.count("user") - physical0,
        )
        rows.append(row)
        emit(row)
    return rows


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest rank."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def summarise(rows: Sequence[Row]) -> Row:
    """One case's counts and per-hook latency (proven and late echoes, ms)."""
    outcomes = Counter(str(r["outcome"]) for r in rows)
    hooks: dict[str, Row] = {}
    for hook in HOOKS:
        latencies = [
            float(r["hooks"][hook]["latency_ms"])
            for r in rows
            if r["hooks"][hook]["latency_ms"] is not None
        ]
        counts = Counter(str(r["hooks"][hook]["outcome"]) for r in rows)
        hooks[hook] = {
            "proven": counts[PROVEN],
            "late": counts[LATE],
            "lost": counts[LOST],
            "p50_ms": percentile(latencies, 0.5),
            "p95_ms": percentile(latencies, 0.95),
            "max_ms": max(latencies) if latencies else None,
        }
    disagree = sum(1 for r in rows if (r["outcome"] != PROVEN) != bool(r["monitor_lost"]))
    return {
        "probes": len(rows),
        "outcomes": {PROVEN: outcomes[PROVEN], LATE: outcomes[LATE], LOST: outcomes[LOST]},
        "hooks": hooks,
        "own_classed_foreign": sum(int(r["own_classed_foreign"]) for r in rows),
        "merged_move_suspects": sum(1 for r in rows if r["merged_move_suspect"]),
        "monitor_disagrees": disagree,
    }


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def format_table(summaries: dict[str, Row], header: Row) -> str:
    lines = [
        f"Windows build {header['windows_build']}  deadline {header['deadline_s']:.3f} s  "
        f"hook timeout {header['hook_timeout_ms']}  interval {header['interval_ms']} ms",
        f"{'case':<14}{'hook':<10}{'p50 ms':>8}{'p95 ms':>8}{'max ms':>8}"
        f"{'proven':>8}{'late':>6}{'lost':>6}{'foreign':>9}{'merged':>8}",
    ]
    for case, summary in summaries.items():
        for i, hook in enumerate(HOOKS):
            h = summary["hooks"][hook]
            tail = (
                f"{summary['own_classed_foreign']:>9}{summary['merged_move_suspects']:>8}"
                if i == 0
                else ""
            )
            lines.append(
                f"{case if i == 0 else '':<14}{hook:<10}{_ms(h['p50_ms']):>8}{_ms(h['p95_ms']):>8}"
                f"{_ms(h['max_ms']):>8}{h['proven']:>8}{h['late']:>6}{h['lost']:>6}{tail}"
            )
    return "\n".join(lines)


# ---- the two real monitors (Windows) ---------------------------------------------------------


class HookReads:
    """`HookMonitor`'s classifier, read as the ledger reads it (one probe's proof per hook)."""

    def __init__(self, monitor: Any) -> None:  # Any: HookMonitor, built on Windows only
        self._classifier = monitor._core._classifier
        self._monitor = monitor

    def now(self) -> float:
        return time.perf_counter()

    def proof(self, hook: str) -> int:
        return int(self._classifier.proof(hook))

    def proof_at(self, hook: str) -> float:
        return float(self._classifier.proof_at(hook))

    def count(self, kind: str) -> int:
        return int(self._monitor.counts().get(kind, 0))


class HelperReads:
    """`HelperMonitor`'s shared status words: proof tags, QPC proof times and the counters."""

    def __init__(
        self, monitor: Any, qpc: Callable[[], int], freq: int
    ) -> None:  # Any: HelperMonitor
        self._status = monitor._helper.status
        self._qpc, self._freq = qpc, freq

    def now(self) -> float:
        return self._qpc() / self._freq

    def proof(self, hook: str) -> int:
        return int(self._status.get(f"proof_{hook}"))

    def proof_at(self, hook: str) -> float:
        return int(self._status.get(f"proof_qpc_{hook}")) / self._freq

    def count(self, kind: str) -> int:
        return int(self._status.get(f"count_{kind}"))


def windows_build() -> str:
    return f"{sys.getwindowsversion().build}"  # type: ignore[attr-defined,unused-ignore]  # win32 only


def _prompt(case: str, probes: int, interval_s: float, ask: Callable[[str], str]) -> None:
    seconds = math.ceil(probes * interval_s) + MOVE_MARGIN_S
    if case.endswith("moving"):
        ask(
            f"[{case}] move the mouse continuously now for ~{seconds} seconds; "
            "press Enter to start, then keep moving it until the case ends: "
        )
    else:
        ask(
            f"[{case}] do not touch the keyboard or the mouse for ~{seconds} seconds "
            "after you press Enter: "
        )


def run_live(  # noqa: PLR0913 - keyword seams  # pragma: no cover - live, Windows only
    case: str,
    *,
    probes: int,
    interval_s: float,
    kill_key: str,
    emit: Callable[[Row], None],
    ask: Callable[[str], str],
) -> tuple[list[Row], Row]:
    from steerai.loop2.live_world import WindowsPlatform, _run_tag  # noqa: PLC0415
    from steerai.session.helper_monitor import qpc_clock  # noqa: PLC0415
    from steerai.session.monitor import probe_deadline_s  # noqa: PLC0415

    _prompt(case, probes, interval_s, ask)
    time.sleep(SETTLE_S)  # the Enter key's own release
    platform = WindowsPlatform()
    platform.dpi_aware()
    timeout_ms = platform.hook_timeout_ms()
    deadline = probe_deadline_s(timeout_ms)
    tag = _run_tag()
    monitor: Any  # Any: HookMonitor or HelperMonitor, Windows only
    if case.startswith("hook"):
        monitor = platform.start_monitor(kill_key, time.perf_counter, tag, deadline)
        reads: Reads = HookReads(monitor)
    else:
        monitor = platform.start_helper_monitor(
            kill_key, time.perf_counter, tag, deadline, timeout_ms
        )
        qpc, freq = qpc_clock()
        reads = HelperReads(monitor, qpc, freq)
    info = {
        "deadline_s": deadline,
        "hook_timeout_ms": "unset" if timeout_ms is None else timeout_ms,
    }
    try:
        rows = run_case(
            case, monitor, reads, probes=probes, interval_s=interval_s, deadline_s=deadline,
            emit=emit,
        )  # fmt: skip
    finally:
        monitor.close()
    return rows, info


def main(argv: Sequence[str] | None = None) -> int:
    from steerai.session.monitor import DEFAULT_KILL_KEY  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m steerai.loop2.hook_tags_probe")
    parser.add_argument("--i-am-present", action="store_true", dest="present")
    parser.add_argument("--out", required=True, help="a directory for the .jsonl and the summary")
    parser.add_argument("--probes", type=int, default=500)
    parser.add_argument("--interval-ms", type=float, default=150.0)
    parser.add_argument("--cases", nargs="+", default=list(CASES), help="space or comma separated")
    parser.add_argument("--kill-key", default=DEFAULT_KILL_KEY)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    cases = [c for item in args.cases for c in item.split(",") if c]
    unknown = [c for c in cases if c not in CASES]
    if unknown:
        parser.error(f"unknown case {unknown[0]!r}; choose from {', '.join(CASES)}")
    args.cases = cases
    if not args.present:
        sys.stderr.write("refused: this probe injects input; run it with --i-am-present\n")
        return 2
    if args.probes < 1 or args.interval_ms < 50:
        sys.stderr.write("refused: --probes >= 1 and --interval-ms >= 50\n")
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log = out / f"hook-tags-{stamp}.jsonl"
    summaries: dict[str, Row] = {}
    header: Row = {
        "windows_build": windows_build(),
        "interval_ms": args.interval_ms,
        "probes_per_case": args.probes,
        "deadline_s": 0.0,
        "hook_timeout_ms": "unset",
    }
    with log.open("w", encoding="utf-8") as handle:

        def emit(row: Row) -> None:
            handle.write(json.dumps(row) + "\n")
            handle.flush()

        emit({"header": header})
        try:
            for case in args.cases:
                rows, info = run_live(
                    case, probes=args.probes, interval_s=args.interval_ms / 1000,
                    kill_key=args.kill_key, emit=emit, ask=input,
                )  # fmt: skip
                header.update(info)
                summaries[case] = summarise(rows)
        except Killed:
            sys.stderr.write("stopped: the kill chord was pressed\n")
            return 3
        emit({"summary": summaries, "header": header})
    table = format_table(summaries, header)
    (out / f"hook-tags-{stamp}-summary.txt").write_text(table + "\n", encoding="utf-8")
    sys.stdout.write(table + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
