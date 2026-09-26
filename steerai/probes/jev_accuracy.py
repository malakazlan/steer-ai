"""Phase 0 probe: Jev element-Choice accuracy on a labelled synthetic inbox page.

Every case's target is known by construction (the page is ours), so labels are exact. Negative
cases expect `none`. Requires TYPESAFE_API_KEY.

Usage (Windows):
    uv run python -m steerai.probes.jev_accuracy [--gate 0.85] [--out r.json]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from steerai.jev import JevClient
from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome
from steerai.probes.element_list import Element, elements_from_nodes, format_lines, prune
from steerai.probes.stats import LatencyStats
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.windows import wait_window_for_pid


@dataclass(frozen=True, slots=True)
class Case:
    intent: str
    role: str | None
    name: str | None


@dataclass(frozen=True, slots=True)
class Outcome:
    intent: str
    expected: int | None
    chosen: int | None
    confidence: float
    margin: float
    latency_ms: float

    @property
    def correct(self) -> bool:
        return self.expected == self.chosen


def load_cases() -> list[Case]:
    raw = resources.files("steerai.probes.pages").joinpath("inbox_cases.json").read_text("utf-8")
    return [Case(intent=c["intent"], role=c["role"], name=c["name"]) for c in json.loads(raw)]


def resolve_expected_id(case: Case, elements: Sequence[Element]) -> int | None:
    if case.role is None:
        return None
    matches = [e for e in elements if e.role == case.role and e.name == case.name]
    if not matches:
        raise LookupError(f"no element {case.role} {case.name!r} for intent {case.intent!r}")
    if len(matches) > 1:
        raise LookupError(f"{len(matches)} elements match {case.role} {case.name!r}")
    return matches[0].id


def summarize(outcomes: Sequence[Outcome], *, gate: float) -> dict[str, Any]:
    n = len(outcomes)
    gated = [o for o in outcomes if o.confidence >= gate]
    stats = LatencyStats.from_samples([o.latency_ms for o in outcomes])
    return {
        "n": n,
        "accuracy": sum(o.correct for o in outcomes) / n,
        "gate": gate,
        "gated_coverage": len(gated) / n,
        "gated_accuracy": (sum(o.correct for o in gated) / len(gated)) if gated else 0.0,
        "latency_p50_ms": stats.p50_ms,
        "latency_p95_ms": stats.p95_ms,
    }


@dataclass(frozen=True, slots=True)
class ProbeArgs:
    chrome: Path
    gate: float
    out: str | None


def parse_args(argv: Sequence[str]) -> ProbeArgs:
    parser = argparse.ArgumentParser(description="Jev element-Choice accuracy on the inbox page.")
    parser.add_argument("--chrome", type=Path, default=CHROME_DEFAULT)
    parser.add_argument("--gate", type=float, default=0.85)
    parser.add_argument("--out", default=None)
    ns = parser.parse_args(argv)
    return ProbeArgs(chrome=ns.chrome, gate=ns.gate, out=ns.out)


def run_probe(args: ProbeArgs) -> dict[str, Any]:
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise OSError("TYPESAFE_API_KEY is not set")
    cases = load_cases()
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-jev-"))
    page = profile_dir / "inbox.html"
    page.write_text(
        resources.files("steerai.probes.pages").joinpath("inbox.html").read_text("utf-8"),
        encoding="utf-8",
    )
    chrome = launch_chrome(args.chrome, profile_dir, 9335, page.as_uri())
    try:
        window = wait_window_for_pid(chrome.pid, title_contains="Google Chrome")
        client = UiaClient()
        tree = _stable_tree(client, window.hwnd)
        elements = elements_from_nodes(tree)
        kept = prune(elements, cap=150)
        lines = format_lines(kept, remaining=len(prune(elements, cap=len(elements))) - len(kept))
        jev = JevClient(api_key=api_key)
        outcomes: list[Outcome] = []
        for case in cases:
            expected = resolve_expected_id(case, kept)
            answer = jev.choose_element(intent=case.intent, lines=lines)
            outcomes.append(
                Outcome(
                    intent=case.intent,
                    expected=expected,
                    chosen=answer.element_id,
                    confidence=answer.confidence,
                    margin=answer.margin,
                    latency_ms=answer.latency_ms,
                )
            )
    finally:
        chrome.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            chrome.wait(timeout=10)
        shutil.rmtree(profile_dir, ignore_errors=True)
    return {
        "element_lines": len(lines),
        "summary": summarize(outcomes, gate=args.gate),
        "outcomes": [asdict(o) | {"correct": o.correct} for o in outcomes],
        "screen": lines,
    }


def _stable_tree(client: UiaClient, hwnd: int) -> list[Any]:
    # Poll until three equal node counts (design 5.1 v0.6); returns RawNode list.
    import time  # noqa: PLC0415 - local to keep the probe's imports minimal

    counts: list[int] = []
    nodes: list[Any] = []
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        nodes = client.cached_tree(hwnd).nodes
        counts.append(len(nodes))
        if len(counts) >= 3 and counts[-1] == counts[-2] == counts[-3]:
            return nodes
        time.sleep(0.25)
    return nodes


def format_report(result: dict[str, Any]) -> str:
    s = result["summary"]
    lines = [
        f"cases {s['n']}  element lines {result['element_lines']}",
        f"accuracy {s['accuracy']:.1%}   gate>={s['gate']}: coverage {s['gated_coverage']:.1%}, "
        f"accuracy {s['gated_accuracy']:.1%}",
        f"latency p50 {s['latency_p50_ms']:.0f} ms  p95 {s['latency_p95_ms']:.0f} ms",
        "wrong:",
    ]
    lines += [
        f"  {o['intent']!r}: expected {o['expected']} got {o['chosen']} "
        f"(conf {o['confidence']:.2f}, margin {o['margin']:.2f})"
        for o in result["outcomes"]
        if not o["correct"]
    ]
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
