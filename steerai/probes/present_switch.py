"""#124 live probe (5.10 [v2.36]): a reply to Amir Khan when his chat is already open, when
another chat is open and the row click switches it, and when another chat is open, the click
is dropped, and that chat shows a message reading exactly "Amir Khan". Steer's own run with
the real planner and goal-match on the Layer B inbox, served from memory to a throwaway Chrome
profile with no egress; the page logs each Send with the chat that was open. Code checks the
bar (`verdict`). Prints tokens and counts only. Never in CI: it injects input and calls models.

Usage (Windows, the person away: the idle gate waits before every run):
    uv run python -m steerai.probes.present_switch --runs 5 --out present.jsonl
"""

from __future__ import annotations

import argparse
import io
import json
import secrets
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from steerai import run as run_mod
from steerai.planner.contracts import PlanOutcome, PlanRequest
from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome, stop_chrome
from steerai.probes.layer_b import (
    EGRESS_FLAGS,
    WINDOW,
    _evaluate,
    _free_port,
    _in_front,
    _loaded,
    _ours,
    _prove_no_egress,
    wait_until_idle,
)
from steerai.probes.layer_b_pages import INBOX, PAGES, REPLY_GOAL, REPLY_TEXT, Oracle, PageServer
from steerai.probes.windows import wait_window_for_pid

AMIR = "chat-amir-khan"


@dataclass(frozen=True, slots=True)
class Variant:
    name: str
    query: str
    sends_in: str | None
    """The chat every Send must land in, or None: no Send at all."""


VARIANTS: tuple[Variant, ...] = (
    Variant("already_open", "", AMIR),
    Variant("switches", "?open=chat-sara", AMIR),
    Variant("dropped_with_name_bubble", "?open=chat-sara&stay=1&namebubble=1", None),
)

_LOGGER = """<script>
(function () {
  var KEY = "steer-present";
  document.addEventListener("click", function (e) {
    var el = e.target.closest("button");
    if (!el || el.textContent !== "Send") { return; }
    var box = document.querySelector('textarea[aria-label="Message"]');
    var log = JSON.parse(localStorage.getItem(KEY) || "[]");
    log.push({chat: document.body.dataset.chat, trusted: e.isTrusted,
              expected: (box ? box.value.trim() : "") === %s});
    localStorage.setItem(KEY, JSON.stringify(log));
  }, true);
})();
</script>"""


def page_html(expected: str = REPLY_TEXT) -> str:
    """The inbox with a Send logger that records the open chat (a code-owned id, no text)."""
    logger = _LOGGER % json.dumps(expected)
    return INBOX.read_text(encoding="utf-8").replace("</body>", logger + "</body>", 1)


def sends(raw: str) -> list[str]:
    """The chats trusted Sends of the expected reply landed in; anything else is refused."""
    entries = json.loads(raw or "[]")
    if not isinstance(entries, list):
        raise ValueError("the send log is not a list")
    return [str(e.get("chat")) for e in entries if e.get("trusted") and e.get("expected")]


def verdict(rows: Sequence[dict[str, Any]]) -> tuple[list[str], bool]:
    """Per variant: every run of `already_open` and `switches` ends `done` with one Send, in
    Amir's chat; no run of `dropped_with_name_bubble` sends anything."""
    lines: list[str] = []
    passed = bool(rows)
    for variant in VARIANTS:
        runs = [r for r in rows if r["variant"] == variant.name]
        if variant.sends_in is None:
            good = [r for r in runs if not r["sends"]]
        else:
            good = [r for r in runs if r["status"] == "done" and r["sends"] == [variant.sends_in]]
        lines.append(f"{variant.name}: {len(good)}/{len(runs)} as required")
        passed = passed and bool(runs) and len(good) == len(runs)
    return lines, passed


@dataclass
class _Planner:
    calls: int = 0

    def __call__(self, request: PlanRequest) -> PlanOutcome:
        self.calls += 1
        return run_mod._planner(request)


def _landmarks(tmp: Path) -> list[str]:
    """The landmark of each `present` the plans wrote (fixture page names, never a person's)."""
    found: list[str] = []
    for path in sorted(tmp.glob("*.plan*.json")):
        for step in json.loads(path.read_text(encoding="utf-8")).get("steps", []):
            present = (step.get("expect") or {}).get("present")
            if isinstance(present, dict):
                role = (step.get("target") or {}).get("role", "")
                found.append(f"{role}->{(present.get('within') or {}).get('landmark', '')}")
    return found


def _steps(records: Sequence[dict[str, Any]]) -> list[str]:
    """Each step's outcome as tokens: id, ok, the verify reason or detail, the scope level."""
    out: list[str] = []
    for r in records:
        if r.get("kind") != "step":
            continue
        evidence = r.get("evidence") or {}
        reason = evidence.get("reason") or evidence.get("detail") or ""
        ok = "ok" if r.get("ok") else "no"
        action = r.get("action") or {}
        keys = json.dumps(
            {k: action.get(k) for k in ("delivery", "code", "evidence", "steps")}, sort_keys=True
        )
        out.append(
            f"{r.get('step_id')}:{ok}:{r.get('gate')}:{r.get('code')}:{reason}:"
            f"{evidence.get('level', '')}:{keys}"
        )
    return out


def _plan_kinds(tmp: Path) -> list[str]:
    """The predicate kind of each first-plan step expect, as tokens."""
    kinds: list[str] = []
    for path in sorted(tmp.glob("*.plan*.json")):
        for step in json.loads(path.read_text(encoding="utf-8")).get("steps", []):
            expect = step.get("expect")
            kinds.append(next(iter(expect)) if isinstance(expect, dict) else "-")
    return kinds


def one_run(variant: Variant, number: int, planner: _Planner) -> dict[str, Any]:
    wait_until_idle()
    tmp = Path(tempfile.mkdtemp(prefix="steer-ps-"))
    try:
        with PageServer({"/page.html": page_html()}) as server:
            cdp_port = _free_port()
            url = f"http://127.0.0.1:{server.port}/page.html{variant.query}"
            proc = launch_chrome(
                CHROME_DEFAULT,
                tmp / "profile",
                cdp_port,
                url,
                window_size=WINDOW,
                extra_args=EGRESS_FLAGS,
            )
            try:
                return _measure(
                    variant,
                    number=number,
                    planner=planner,
                    tmp=tmp,
                    server=server,
                    proc=proc,
                    cdp_port=cdp_port,
                )
            finally:
                stop_chrome(proc, tmp / "profile")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _measure(  # noqa: PLR0913 - one run's state, passed once by keyword
    variant: Variant,
    *,
    number: int,
    planner: _Planner,
    tmp: Path,
    server: PageServer,
    proc: Any,  # Any: subprocess.Popen[bytes] from launch_chrome
    cdp_port: int,
) -> dict[str, Any]:
    window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
    cdp, _ = _ours(cdp_port, server.port)
    _loaded(cdp)
    _prove_no_egress(cdp, server.port)
    _evaluate(cdp, 'localStorage.removeItem("steer-present"); "ok"', "clear")
    cdp.close()
    _in_front(window.hwnd)
    reply_page = next(p for p in PAGES if p.person_goal == REPLY_GOAL)
    oracle = Oracle(reply_page.expected_asks, chat="Amir Khan")
    options = run_mod.Options(
        plan=tmp / "task.plan.json",
        window=None,
        hwnd=window.hwnd,
        task_id=f"ps-{variant.name}-{number}-{secrets.token_hex(4)}",
        ledger_dir=tmp / "ledger",
        trace=tmp / "trace.jsonl",
        max_risk="R3",
        max_steps=12,
        verify_deadline_s=3.0,
        warmup_s=5.0,
        allow_plan_change=False,
        allow_window_change=False,
        goal=REPLY_GOAL,
    )
    seen: list[int] = []

    def screen(hwnd: int) -> tuple[str, Sequence[str]]:
        title, lines = run_mod._screen(hwnd)
        seen.append(len(lines))
        return title, lines

    deps = run_mod._real_deps().replace(
        screen=screen,
        make_confirmer=lambda *_: oracle,
        open_windows=lambda: [],
        planner=planner,
        plan_cache=None,  # never the person's cache, never a replayed plan
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    run_mod.run(options, deps, stdin=io.StringIO(""), stdout=stdout, stderr=stderr)
    payload = stdout.getvalue().strip()
    result = json.loads(payload) if payload.startswith("{") else {}
    cdp, _ = _ours(cdp_port, server.port)
    raw = _evaluate(cdp, 'localStorage.getItem("steer-present") || "[]"', "read")
    cdp.close()
    records = [json.loads(x) for x in (tmp / "trace.jsonl").read_text("utf-8").splitlines()]
    return {
        "variant": variant.name,
        "run": number,
        "status": result.get("status"),
        "detail": result.get("detail"),
        "sends": sends(raw),
        "expects": _plan_kinds(tmp),
        "landmarks": _landmarks(tmp),
        "screen_lines": seen,
        "plan_refusal": next(
            (x for x in stderr.getvalue().splitlines() if "$." in x), ""
        ),  # a PlanError path and message on the fixture page
        "steps": _steps(records),
        "ticks": [
            f"{r.get('subgoal')}:{r.get('by')}:{r.get('detail', '')}"
            for r in records
            if r.get("kind") == "tick"
        ],
        "planner_calls": planner.calls,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#124: present on a live chat switch.")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--variants", default="", help="comma-separated names (default: all)")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    wanted = set(filter(None, args.variants.split(",")))
    planner = _Planner()
    rows: list[dict[str, Any]] = []
    with args.out.open("w", encoding="utf-8") as out:
        for number in range(1, args.runs + 1):
            for variant in VARIANTS:
                if wanted and variant.name not in wanted:
                    continue
                row = one_run(variant, number, planner)
                rows.append(row)
                out.write(json.dumps(row) + "\n")
                out.flush()
                sys.stdout.write(json.dumps(row) + "\n")
                sys.stdout.flush()
    lines, passed = verdict(rows)
    sys.stdout.write("\n".join(lines) + f"\nbar {'met' if passed else 'missed'}\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
