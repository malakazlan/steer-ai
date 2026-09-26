"""Phase 0 probe: UIA subtree snapshot latency and node count per window.

Usage (Windows):
    uv run python -m steerai.probes.uia_latency --title Chrome --title Explorer --out r.json

Answers design question 12/Phase 0: is "UIA window snapshot < 200 ms" true per app type?
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from steerai.probes.stats import LatencyStats
from steerai.probes.uia_snapshot import Snapshot, UiaClient
from steerai.probes.windows import find_window


@dataclass(frozen=True, slots=True)
class ProbeArgs:
    titles: list[str]
    runs: int
    warmup: int
    out: str | None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    title: str
    hwnd: int
    pid: int
    stats: LatencyStats
    last: Snapshot

    def to_dict(self) -> dict[str, Any]:
        # JSON payload; values are str, int, float and a str->int map.
        return {
            "title": self.title,
            "hwnd": self.hwnd,
            "pid": self.pid,
            "runs": self.stats.n,
            "p50_ms": self.stats.p50_ms,
            "p95_ms": self.stats.p95_ms,
            "max_ms": self.stats.max_ms,
            "mean_ms": self.stats.mean_ms,
            "node_count": self.last.node_count,
            "by_control_type": dict(self.last.by_control_type),
        }


def parse_args(argv: Sequence[str]) -> ProbeArgs:
    parser = argparse.ArgumentParser(description="Measure UIA subtree snapshot latency per window.")
    parser.add_argument(
        "--title", dest="titles", action="append", required=True, help="window title substring"
    )
    parser.add_argument("--runs", type=int, default=10, help="timed snapshots per window")
    parser.add_argument("--warmup", type=int, default=2, help="untimed snapshots before measuring")
    parser.add_argument("--out", default=None, help="write results as JSON to this path")
    namespace = parser.parse_args(argv)
    return ProbeArgs(
        titles=list(namespace.titles),
        runs=namespace.runs,
        warmup=namespace.warmup,
        out=namespace.out,
    )


def probe_window(client: UiaClient, title: str, *, runs: int, warmup: int) -> ProbeResult:
    window = find_window(title)
    if window is None:
        raise LookupError(f"no visible window with title containing {title!r}")
    for _ in range(warmup):
        client.snapshot_subtree(window.hwnd)
    samples: list[float] = []
    last: Snapshot | None = None
    for _ in range(runs):
        last = client.snapshot_subtree(window.hwnd)
        samples.append(last.elapsed_ms)
    if last is None:
        raise ValueError("runs must be at least 1")
    return ProbeResult(
        title=window.title,
        hwnd=window.hwnd,
        pid=window.pid,
        stats=LatencyStats.from_samples(samples),
        last=last,
    )


def format_report(results: Sequence[ProbeResult]) -> str:
    header = f"{'window':<48} {'nodes':>7} {'p50 ms':>8} {'p95 ms':>8} {'max ms':>8} {'runs':>5}"
    lines = [header, "-" * len(header)]
    for result in results:
        title = result.title if len(result.title) <= 48 else result.title[:45] + "..."
        lines.append(
            f"{title:<48} {result.last.node_count:>7} {result.stats.p50_ms:>8.1f} "
            f"{result.stats.p95_ms:>8.1f} {result.stats.max_ms:>8.1f} {result.stats.n:>5}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    client = UiaClient()
    results: list[ProbeResult] = []
    for title in args.titles:
        try:
            results.append(probe_window(client, title, runs=args.runs, warmup=args.warmup))
        except LookupError as exc:
            sys.stderr.write(f"skip: {exc}\n")
    if not results:
        sys.stderr.write("no windows probed\n")
        return 1
    sys.stdout.write(format_report(results) + "\n")
    if args.out is not None:
        Path(args.out).write_text(
            json.dumps([r.to_dict() for r in results], indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
