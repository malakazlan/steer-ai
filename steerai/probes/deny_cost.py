"""#83 probe: the cost of `effective_risk` with the closure matcher, cold (the cell cache cleared
before every call), on ordinary and adversarial names. Pure CPU, no input, no window.

Usage:
    uv run python -m steerai.probes.deny_cost --runs 200

Answers 5.13 1a [v2.26] / section 9: p50 and p95 per case, and whether the 10k-character cost
grows linearly from 1k (the ratio near 10, not 100).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence

from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.gate import effective_risk
from steerai.probes.stats import LatencyStats
from steerai.verify.deny_skeleton import cells

SOFT_I = chr(0x00A1)
CGJ = chr(0x034F)
CANDIDATES = 12
"""The person ceiling's cap (`steerai.world.people.MAX_CANDIDATES`): 12 bound names per step."""


def _step(name: str, automation_id: str = "") -> Step:
    target = Target(role="Button", name=name, automation_id=automation_id)
    return Step(id="c1", subgoal="g1", verb="click", target=target, risk="R0")


def _cases(length: int) -> dict[str, str]:
    prose = ("Open the latest results for this channel " * (length // 41 + 1))[:length]
    return {
        f"prose {length}": prose,
        f"SIGN repeated {length}": ("SIGN" * (length // 4 + 1))[:length],
        f"blank run {length}": "Se" + CGJ * (length - 4) + "nd",
        f"soft run {length}": "sign" + SOFT_I * (length - 7) + "out",
    }


def _risk_of(step: Step) -> Callable[[], object]:
    return lambda: effective_risk(step)


def _time(call: Callable[[], object], runs: int) -> LatencyStats:
    samples: list[float] = []
    for _ in range(runs):
        cells.cache_clear()
        start = time.perf_counter()
        call()
        samples.append((time.perf_counter() - start) * 1000.0)
    return LatencyStats.from_samples(samples)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="effective_risk cost with the closure matcher.")
    parser.add_argument("--runs", type=int, default=200)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    rows: list[tuple[str, LatencyStats]] = []
    one = _step("Send message", automation_id="btnSendMessage")
    rows.append(("one name and id", _time(lambda: effective_risk(one), args.runs)))
    names = [f"Contact {index} {SOFT_I} hi there" for index in range(CANDIDATES)]
    person = _step("Amir")
    rows.append(
        (
            f"{CANDIDATES} bound names",
            _time(lambda: [effective_risk(person, name) for name in names], args.runs),
        )
    )
    by_case: dict[str, LatencyStats] = {}
    for length in (1_000, 10_000):
        for label, text in _cases(length).items():
            by_case[label] = _time(_risk_of(_step(text)), max(args.runs // 10, 5))
            rows.append((label, by_case[label]))
    sys.stdout.write(f"{'case':<28} {'p50 ms':>9} {'p95 ms':>9} {'n':>5}\n")
    for label, stats in rows:
        sys.stdout.write(f"{label:<28} {stats.p50_ms:>9.3f} {stats.p95_ms:>9.3f} {stats.n:>5}\n")
    for label in _cases(0):
        kind = label.rsplit(" ", 1)[0]
        ratio = by_case[f"{kind} 10000"].p50_ms / by_case[f"{kind} 1000"].p50_ms
        sys.stdout.write(f"10k/1k p50 ratio, {kind}: {ratio:.1f}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
