"""What listing offscreen elements costs the planner (offscreen-lines packet, principal REVISE item
4): for each page, the screen lines without and with offscreen elements, the planner prompt's
input tokens for each (`messages.count_tokens` on the exact system and user turns), and the
planner's own latency and outcome for the page's goal, n runs per variant.

Pages: the development set's Chrome starts (Google, YouTube, Wikipedia, the probe inbox), the
spent H2 page with l1's goal (the below-the-fold "Load more" that made the gap visible), and
Notepad, whose lines must not change (OS-53). Throwaway profiles, idle-gated, no input. The
output holds counts, tokens, latencies, verbs and roles, never page names or plan text.

Usage (Windows, needs ANTHROPIC_API_KEY):
    uv run python -m steerai.probes.lines_cost --runs 5
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome, page_ws_url, stop_chrome
from steerai.probes.layer_b import WINDOW, _free_port, _in_front, wait_until_idle
from steerai.probes.layer_b_pages import PageServer
from steerai.probes.stats import LatencyStats
from steerai.world.elements import format_lines, prune
from steerai.world.model import Snapshot

NOTEPAD = Path(r"C:\Windows\System32\notepad.exe")
FENCE = "f" * 32
"""A fixed fence so both variants' prompts differ only in their lines."""


@dataclass(frozen=True, slots=True)
class Page:
    name: str
    goal: str
    start: str
    """A URL, `inbox`, `h2` or `notepad`."""


PAGES = (
    Page(
        "google", "search Google for steer ai computer use agent", "https://www.google.com/?hl=en"
    ),
    Page("youtube", "search YouTube for lofi hip hop", "https://www.youtube.com/"),
    Page(
        "wikipedia",
        "search Wikipedia for Alan Turing",
        "https://en.wikipedia.org/wiki/Main_Page",
    ),
    Page("inbox", "open the chat with Sara Ahmed", "inbox"),
    Page("h2", "load more items in the feed", "h2"),
    Page("notepad", "type hello from steer in notepad", "notepad"),
)


def uncapped_lines(snapshot: Snapshot) -> list[str]:
    """The lines as they were before the capability tokens (packet interaction-model-2026-11-12):
    the same snapshot with every element's tokens removed."""
    return replace(snapshot, elements=[replace(e, caps=()) for e in snapshot.elements]).lines


def before_lines(snapshot: Snapshot) -> list[str]:
    """The lines as `Snapshot.lines` built them before D-OS v2: on-screen elements only."""
    kept = prune(snapshot.elements, cap=snapshot.cap)
    eligible = prune(snapshot.elements, cap=max(len(snapshot.elements), 1))
    return format_lines(kept, remaining=len(eligible) - len(kept))


def _observe(hwnd: int) -> Snapshot:
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.run import warm_up  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    model = WorldModel(UiaClient())
    held: list[Snapshot] = []

    def observe() -> Snapshot:
        held.append(model.observe(hwnd, cap_s=5.0))
        return held[-1]

    warm_up(observe, cap_s=15.0)
    return held[-1] if held else model.observe(hwnd, cap_s=5.0)


@contextmanager
def _window(page: Page, tmp: Path) -> Iterator[tuple[int, str]]:
    """The page's window handle and title, closed afterwards."""
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415 - Windows only

    if page.start == "notepad":
        proc = subprocess.Popen([str(NOTEPAD)])  # noqa: S603 - a fixed system path
        try:
            window = wait_window_for_pid(proc.pid, image="notepad.exe", process=proc)
            _in_front(window.hwnd)
            yield window.hwnd, "notepad"
        finally:
            proc.kill()
        return
    pages: dict[str, str] = {}
    if page.start == "inbox":
        from steerai.probes.present_switch import page_html  # noqa: PLC0415

        pages = {"/page.html": page_html("ok thanks")}
    elif page.start == "h2":
        from steerai.probes.h2_page import page_html as h2_html  # noqa: PLC0415 - H2 only

        pages = {"/page.html": h2_html()}
    with PageServer(pages) as server:
        url = page.start if not pages else f"http://127.0.0.1:{server.port}/page.html"
        port = _free_port()
        proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url, window_size=WINDOW)
        try:
            window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
            page_ws_url(port)
            time.sleep(3.0)  # a real site settles its first layout (unmeasured)
            _in_front(window.hwnd)
            yield window.hwnd, "chrome"
        finally:
            stop_chrome(proc, tmp / "profile")


def _tokens(count: Callable[..., Any], model: str, goal: str, lines: Sequence[str]) -> int:
    from steerai.planner.claude_planner import build_prompt  # noqa: PLC0415
    from steerai.planner.contracts import PlanRequest  # noqa: PLC0415

    system, user = build_prompt(PlanRequest(goal=goal, screen_lines=lines), tag=FENCE)
    result = count(model=model, system=system, messages=[{"role": "user", "content": user}])
    return int(result.input_tokens)


def _plans(
    planner: Any, goal: str, lines: Sequence[str], runs: int
) -> dict[str, Any]:  # Any: a planner
    """n planner calls: latency, and per call the outcome, step verbs and target roles, and
    whether the first target is an offscreen line."""
    from steerai.planner.contracts import PlannerError, PlanRequest  # noqa: PLC0415

    offscreen = {
        line.split('"')[1] for line in lines if line.endswith(" offscreen") and '"' in line
    }
    samples: list[float] = []
    calls: list[dict[str, Any]] = []
    for _ in range(runs):
        started = time.perf_counter()
        try:
            outcome = planner.plan(PlanRequest(goal=goal, screen_lines=lines))
        except PlannerError as exc:
            samples.append((time.perf_counter() - started) * 1000)
            calls.append({"outcome": exc.detail})
            continue
        samples.append((time.perf_counter() - started) * 1000)
        targeted = [(s.verb, s.target) for s in outcome.plan.steps if s.target is not None]
        calls.append(
            {
                "outcome": "plan",
                "steps": [[verb, target.role] for verb, target in targeted],
                "first_offscreen": bool(targeted) and targeted[0][1].name in offscreen,
                "input_tokens": outcome.input_tokens,
            }
        )
    stats = LatencyStats.from_samples(samples)
    return {"p50_ms": round(stats.p50_ms), "p95_ms": round(stats.p95_ms), "calls": calls}


def measure(page: Page, runs: int, compare: str = "offscreen") -> dict[str, Any]:
    import anthropic  # noqa: PLC0415 - the SDK, only for a live run

    from steerai.planner.claude_planner import ClaudePlanner  # noqa: PLC0415

    planner = ClaudePlanner.from_env()
    count = anthropic.Anthropic().messages.count_tokens
    wait_until_idle()
    tmp = Path(tempfile.mkdtemp(prefix="steer-lines-"))
    try:
        with _window(page, tmp) as (hwnd, _app):
            started = time.perf_counter()
            snapshot = _observe(hwnd)
            observe_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            after = snapshot.lines
            lines_ms = (time.perf_counter() - started) * 1000
        before = before_lines(snapshot) if compare == "offscreen" else uncapped_lines(snapshot)
        row: dict[str, Any] = {
            "page": page.name,
            "compare": compare,
            "capped_lines": sum(bool(e.caps) for e in snapshot.elements),
            "elements": len(snapshot.elements),
            "offscreen_elements": sum(e.offscreen for e in snapshot.elements),
            "lines_before": len(before),
            "lines_after": len(after),
            "offscreen_lines": sum(line.endswith(" offscreen") for line in after),
            "identical": before == after,
            "lines_ms": round(lines_ms, 2),
            "observe_ms": round(observe_ms),
            "tokens_before": _tokens(count, planner.model, page.goal, before),
            "tokens_after": _tokens(count, planner.model, page.goal, after),
        }
        if runs:
            row["before"] = _plans(planner, page.goal, before, runs)
            row["after"] = _plans(planner, page.goal, after, runs)
        return row
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The planner cost of offscreen lines.")
    parser.add_argument("--runs", type=int, default=5, help="planner calls per variant; 0: none")
    parser.add_argument("--page", choices=[p.name for p in PAGES], action="append")
    parser.add_argument(
        "--compare",
        choices=("offscreen", "caps"),
        default="offscreen",
        help="the before variant: no offscreen lines, or no capability tokens",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    for page in PAGES:
        if args.page and page.name not in args.page:
            continue
        sys.stdout.write(json.dumps(measure(page, args.runs, args.compare)) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
