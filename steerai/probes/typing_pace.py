"""Typing-pace probe: the smallest per-chunk delay that drops no code units (design 5.9 [v1.0]).

Types a fixed sample into a Notepad document and a Chrome textarea in `chunk_text` chunks of 8
UTF-16 code units through `type_text` (SendInput, KEYEVENTF_UNICODE), sleeping `delay_ms` between
chunks, and reads the field back through ValuePattern. A trial's drops are the code units the
field never received; the picked delay is the smallest with zero drops on every target.

    uv run python -m steerai.probes.typing_pace [--delays 0,5,10,20,40] [--runs 5] [--units 200]
        [--no-chrome] [--chrome path] [--out r.json]
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
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from steerai.action.actionability import centre_of
from steerai.action.execution import UIA_VALUE_PATTERN, UiaBindings
from steerai.action.keyboard import chunk_text, type_text
from steerai.action.sanitize import normalise_newlines
from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome
from steerai.probes.stats import LatencyStats
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.win_input import acquire_foreground, click_at
from steerai.probes.windows import wait_window_for_pid
from steerai.world.model import Snapshot, WorldModel

DEFAULT_DELAYS_MS = (0, 5, 10, 20, 40)
READBACK_CAP_S = 2.0
_ALPHABET = "the quick brown fox jumps over 13 lazy dogs; café naïve résumé 42 "


@dataclass(frozen=True, slots=True)
class Trial:
    target: str
    delay_ms: int
    units: int
    received: int
    wall_ms: float

    @property
    def drops(self) -> int:
        return max(self.units - self.received, 0)


def make_sample(units: int) -> str:
    """Deterministic text of exactly `units` UTF-16 code units: ASCII plus BMP accents, no
    newlines (ValuePattern readback then compares byte for byte)."""
    repeated = _ALPHABET * (units // len(_ALPHABET) + 1)
    return repeated[:units]


def summarise(trials: Sequence[Trial]) -> dict[str, dict[int, dict[str, Any]]]:
    """{target: {delay_ms: {runs, drops_max, drops_runs, wall_ms_p50, wall_ms_p95}}}."""
    grouped: dict[str, dict[int, list[Trial]]] = {}
    for trial in trials:
        grouped.setdefault(trial.target, {}).setdefault(trial.delay_ms, []).append(trial)
    summary: dict[str, dict[int, dict[str, Any]]] = {}
    for target, by_delay in grouped.items():
        summary[target] = {}
        for delay_ms, rows in sorted(by_delay.items()):
            walls = LatencyStats.from_samples([r.wall_ms for r in rows])
            summary[target][delay_ms] = {
                "runs": len(rows),
                "drops_max": max(r.drops for r in rows),
                "drops_runs": sum(1 for r in rows if r.drops),
                "wall_ms_p50": walls.p50_ms,
                "wall_ms_p95": walls.p95_ms,
            }
    return summary


def pick_delay(summary: dict[str, dict[int, dict[str, Any]]]) -> int | None:
    """Smallest delay measured on every target with zero drops everywhere, else None."""
    if not summary:
        return None
    delays = set.intersection(*(set(rows) for rows in summary.values()))
    clean = [d for d in delays if all(rows[d]["drops_max"] == 0 for rows in summary.values())]
    return min(clean) if clean else None


# ---- live targets (Windows) -------------------------------------------------------------------


@dataclass
class _Target:
    name: str
    hwnd: int
    handle: Any  # live COM element of the text field
    read: Callable[[], str]
    clear: Callable[[], None]
    centre: tuple[int, int]

    def focus(self) -> None:
        acquire_foreground(self.hwnd)
        click_at(*self.centre)
        time.sleep(0.1)


def _value_target(
    name: str,
    snapshot: Snapshot,
    bindings: UiaBindings,
    role_names: set[str],
    field_name: str | None,
) -> _Target:
    element = next(
        e
        for e in snapshot.elements
        if e.role in role_names and (field_name is None or e.name == field_name)
    )
    handle = snapshot.handle(element.id)
    pattern = bindings.pattern(handle, UIA_VALUE_PATTERN, "ValuePattern")

    def read() -> str:
        raw = pattern.CurrentValue
        return "" if raw is None else normalise_newlines(str(raw))

    return _Target(
        name=name,
        hwnd=snapshot.hwnd,
        handle=handle,
        read=read,
        clear=lambda: pattern.SetValue(""),
        centre=centre_of(element.box),
    )


def _type_sample(target: _Target, sample: str, delay_ms: int) -> Trial:
    target.clear()
    deadline = time.monotonic() + READBACK_CAP_S
    while target.read() != "" and time.monotonic() < deadline:
        time.sleep(0.02)
    if target.read() != "":
        raise RuntimeError(f"{target.name}: field did not clear")
    start = time.perf_counter()
    for chunk in chunk_text(sample):
        type_text(chunk)
        if delay_ms:
            time.sleep(delay_ms / 1000)
    deadline = time.monotonic() + READBACK_CAP_S
    value = target.read()
    while value != sample and time.monotonic() < deadline:
        time.sleep(0.02)
        value = target.read()
    wall_ms = (time.perf_counter() - start) * 1000
    received = len(value.encode("utf-16-le")) // 2
    return Trial(
        target=target.name,
        delay_ms=delay_ms,
        units=len(sample.encode("utf-16-le")) // 2,
        received=received,
        wall_ms=wall_ms,
    )


def _run_target(target: _Target, sample: str, delays: Sequence[int], runs: int) -> list[Trial]:
    target.focus()
    trials: list[Trial] = []
    for delay_ms in delays:
        for _ in range(runs):
            trials.append(_type_sample(target, sample, delay_ms))
    return trials


@dataclass(frozen=True, slots=True)
class ProbeArgs:
    delays: tuple[int, ...]
    runs: int
    units: int
    chrome: Path | None
    out: str | None


def parse_args(argv: Sequence[str]) -> ProbeArgs:
    parser = argparse.ArgumentParser(description="Smallest drop-free per-chunk typing delay.")
    parser.add_argument("--delays", default=",".join(str(d) for d in DEFAULT_DELAYS_MS))
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--units", type=int, default=200)
    parser.add_argument("--chrome", type=Path, default=CHROME_DEFAULT)
    parser.add_argument("--no-chrome", action="store_true")
    parser.add_argument("--out", default=None)
    ns = parser.parse_args(argv)
    delays = tuple(int(d) for d in ns.delays.split(","))
    if any(d < 0 for d in delays) or ns.runs <= 0 or ns.units <= 0:
        parser.error("delays must be >= 0, runs and units > 0")
    return ProbeArgs(
        delays=delays,
        runs=ns.runs,
        units=ns.units,
        chrome=None if ns.no_chrome else ns.chrome,
        out=ns.out,
    )


def run_probe(args: ProbeArgs) -> dict[str, Any]:
    sample = make_sample(args.units)
    client = UiaClient()
    model = WorldModel(client)
    bindings = UiaBindings.load()
    trials: list[Trial] = []

    notepad = subprocess.Popen(["notepad.exe"])  # noqa: S607 - fixed, well-known executable
    try:
        window = wait_window_for_pid(notepad.pid)
        time.sleep(0.5)
        snapshot = model.observe(window.hwnd, cap_s=5.0)
        target = _value_target("notepad", snapshot, bindings, {"Document", "Edit"}, None)
        trials += _run_target(target, sample, args.delays, args.runs)
    finally:
        notepad.terminate()

    if args.chrome is not None:
        if not args.chrome.is_file():
            raise FileNotFoundError(args.chrome)
        profile_dir = Path(tempfile.mkdtemp(prefix="steer-pace-"))
        page = Path(__file__).with_name("pages") / "inbox.html"
        chrome = launch_chrome(args.chrome, profile_dir, 9334, page.as_uri())
        try:
            window = wait_window_for_pid(chrome.pid, title_contains="Google Chrome")
            time.sleep(3.0)
            snapshot = model.observe(window.hwnd, cap_s=20.0)
            target = _value_target("chrome", snapshot, bindings, {"Edit"}, "Message")
            trials += _run_target(target, sample, args.delays, args.runs)
        finally:
            chrome.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                chrome.wait(timeout=10)
            shutil.rmtree(profile_dir, ignore_errors=True)

    summary = summarise(trials)
    return {
        "units": args.units,
        "chunk_units": 8,
        "runs": args.runs,
        "trials": [asdict(t) | {"drops": t.drops} for t in trials],
        "summary": {t: {str(d): row for d, row in rows.items()} for t, rows in summary.items()},
        "picked_delay_ms": pick_delay(summary),
    }


def format_report(result: dict[str, Any]) -> str:
    lines = [f"typing pace: {result['units']} units in chunks of {result['chunk_units']}"]
    for target, rows in result["summary"].items():
        for delay, row in rows.items():
            lines.append(
                f"  {target:8s} delay {delay:>3s} ms  drops max {row['drops_max']} "
                f"({row['drops_runs']}/{row['runs']} runs)  wall p50 {row['wall_ms_p50']:.0f} ms "
                f"p95 {row['wall_ms_p95']:.0f} ms"
            )
    lines.append(f"picked delay: {result['picked_delay_ms']} ms")
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
