"""#150 probe (design D-150): identical twin fields, as on docs.python.org (Sphinx's "Related"
bar in the header and again in the footer, both `Edit "Quick search"` with AutomationId
`search-box`).

Sections (`--section`):
- `scout`: docs.python.org/3/ in a throwaway profile, no input: each `Edit "Quick search"` with
  its box, offscreen flag, AutomationId and ancestor chain, as Steer's world model sees it.
- `fixture`: a local Sphinx-like page with the field in the header and again below a tall
  spacer, run through Steer with a scripted plan (focus, type, submit, py1's shape) and a person
  who approves only the query: the run must end `done` with exactly one search, from the header
  field (the one on screen), carrying the query. No model is called; the page has no egress.

`fixture` injects input: the idle gate waits before every run.

Usage (Windows, the person away):
    uv run python -m steerai.probes.twins --section scout
    uv run python -m steerai.probes.twins --section fixture --runs 5
"""

from __future__ import annotations

import argparse
import io
import json
import secrets
import shutil
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome, page_ws_url, stop_chrome
from steerai.probes.layer_b import (
    EGRESS_FLAGS,
    WINDOW,
    _free_port,
    _in_front,
    _loaded,
    _ours,
    _prove_no_egress,
    wait_until_idle,
)
from steerai.probes.layer_b_pages import PageServer
from steerai.probes.task_suite import Person

QUERY = "pathlib"
SCOUT_URL = "https://docs.python.org/3/"
_FORM = (
    '<form action="/search" method="get" role="search">'
    '<input type="text" name="q" id="search-box" aria-label="Quick search">'
    '<input type="hidden" name="from" value="{where}"><input type="submit" value="Go"></form>'
)
PAGE = (
    "<!doctype html><title>Fixture documentation</title>"
    f'<nav aria-label="Related"><ul><li>{_FORM.format(where="top")}</li></ul></nav>'
    "<main><h1>Fixture documentation</h1><p>Text.</p>"
    '<div style="height:3000px"></div></main>'
    f'<nav aria-label="Related"><ul><li>{_FORM.format(where="bottom")}</li></ul></nav>'
)
"""Sphinx's Related bar holds its search form in a list item, which Chrome names from its
content: "Go" before typing and "<text>Go" after (docs.python.org, 2026-11-08)."""
RESULTS = f"<!doctype html><title>{QUERY} - Search results</title><h1>Search results</h1>"
PLAN = {
    "goal": f"search the fixture docs for {QUERY}",
    "subgoals": [
        {
            "id": "g1",
            "desc": "The quick search field holds the query",
            "condition": {"value": {"role": "Edit", "name": "Quick search", "text": QUERY}},
        },
        {
            "id": "g2",
            "desc": "The search results page replaces the documentation page",
            "condition": {"appeared": {"role": "Document", "name": QUERY, "replaces": True}},
        },
    ],
    "steps": [
        {
            "id": "s1",
            "subgoal": "g1",
            "verb": "focus",
            "target": {"role": "Edit", "name": "Quick search"},
            "risk": "R0",
        },
        {
            "id": "s2",
            "subgoal": "g1",
            "verb": "type",
            "target": {"role": "Edit", "name": "Quick search"},
            "risk": "R1",
            "text": QUERY,
            "text_provenance": "verbatim",
        },
        {
            "id": "s3",
            "subgoal": "g2",
            "verb": "submit",
            "target": {"role": "Edit", "name": "Quick search"},
            "risk": "R2",
            "params": {
                "text": QUERY,
                "composer_role": "Edit",
                "composer_name": "Quick search",
                "scope": "Fixture documentation",
            },
            "expect": {"appeared": {"role": "Document", "name": QUERY, "replaces": True}},
        },
    ],
}


def submitted(paths: Sequence[str]) -> list[tuple[str, str]]:
    """(form, query) of each search request the server saw, in order."""
    found = []
    for path in paths:
        parts = urllib.parse.urlsplit(path)
        if parts.path == "/search":
            params = urllib.parse.parse_qs(parts.query)
            found.append((params.get("from", [""])[0], params.get("q", [""])[0]))
    return found


def verdict(rows: Sequence[dict[str, Any]]) -> tuple[int, bool]:
    """(runs that passed, the bar): every run `done` with exactly one search, from the top
    field, carrying the query."""
    good = sum(
        1 for r in rows if r.get("status") == "done" and r.get("searches") == [["top", QUERY]]
    )
    return good, bool(rows) and good == len(rows)


def scout() -> list[str]:
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="steer-twins-"))
    port = _free_port()
    proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, SCOUT_URL, window_size=WINDOW)
    try:
        window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
        page_ws_url(port)
        time.sleep(5.0)
        snapshot = WorldModel(UiaClient()).observe(window.hwnd, cap_s=8.0)
        by_id = {e.id: e for e in snapshot.elements}
        lines = [f"elements {len(snapshot.elements)}"]
        for element in snapshot.elements:
            if element.role == "Edit" and element.name == "Quick search":
                chain, node = [], element
                while node.parent_id is not None and node.parent_id in by_id:
                    node = by_id[node.parent_id]
                    chain.append(f"{node.role}:{node.name[:20]!r}")
                lines.append(
                    f"twin box={element.box} offscreen={element.offscreen} "
                    f"aid={element.automation_id!r} up={' < '.join(chain[:6])}"
                )
        return lines
    finally:
        stop_chrome(proc, tmp / "profile")
        shutil.rmtree(tmp, ignore_errors=True)


def fixture_run(number: int) -> dict[str, Any]:
    from steerai import run as run_mod  # noqa: PLC0415 - Windows-only runner deps
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415

    wait_until_idle()
    tmp = Path(tempfile.mkdtemp(prefix="steer-twins-"))
    try:
        with PageServer({"/page.html": PAGE, "/search": RESULTS}) as server:
            cdp_port = _free_port()
            url = f"http://127.0.0.1:{server.port}/page.html"
            proc = launch_chrome(
                CHROME_DEFAULT,
                tmp / "profile",
                cdp_port,
                url,
                window_size=WINDOW,
                extra_args=EGRESS_FLAGS,
            )
            try:
                window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
                cdp, _ = _ours(cdp_port, server.port)
                _loaded(cdp)
                _prove_no_egress(cdp, server.port)
                cdp.close()
                _in_front(window.hwnd)
                plan = tmp / "task.plan.json"
                plan.write_text(json.dumps(PLAN), encoding="utf-8")
                person = Person(approve=frozenset({QUERY}))
                options = run_mod.Options(
                    plan=plan,
                    window=None,
                    hwnd=window.hwnd,
                    task_id=f"twins-{number}-{secrets.token_hex(4)}",
                    ledger_dir=tmp / "ledger",
                    trace=tmp / "trace.jsonl",
                    max_risk="R3",
                    max_steps=8,
                    verify_deadline_s=3.0,
                    warmup_s=5.0,
                    allow_plan_change=False,
                    allow_window_change=False,
                    goal=None,
                )
                deps = run_mod._real_deps().replace(
                    make_confirmer=lambda *_: person,
                    open_windows=lambda: [],
                    plan_cache=None,
                )
                stdout = io.StringIO()
                run_mod.run(
                    options, deps, stdin=io.StringIO(""), stdout=stdout, stderr=io.StringIO()
                )
                payload = stdout.getvalue().strip()
                result = json.loads(payload) if payload.startswith("{") else {}
                records = [
                    json.loads(x) for x in (tmp / "trace.jsonl").read_text("utf-8").splitlines()
                ]
                steps = [r for r in records if r.get("kind") == "step"]
                return {
                    "run": number,
                    "status": result.get("status"),
                    "detail": result.get("detail"),
                    "searches": [list(s) for s in submitted(server.paths)],
                    "steps": [
                        f"{s.get('step_id')}:{s.get('ok')}:{s.get('code')}:"
                        f"{s.get('evidence', {}).get('twin', '')}:"
                        f"{s.get('evidence', {}).get('reason', '')}"
                        for s in steps
                    ],
                    "ticks": [
                        f"{r.get('subgoal')}:{r.get('by')}"
                        for r in records
                        if r.get("kind") == "tick"
                    ],
                    "asks": person.log,
                }
            finally:
                stop_chrome(proc, tmp / "profile")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="#150: identical twin fields.")
    parser.add_argument("--section", choices=("scout", "fixture"), required=True)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.section == "scout":
        sys.stdout.write("\n".join(scout()) + "\n")
        return 0
    rows = []
    for number in range(1, args.runs + 1):
        row = fixture_run(number)
        rows.append(row)
        sys.stdout.write(json.dumps(row) + "\n")
        sys.stdout.flush()
    good, met = verdict(rows)
    sys.stdout.write(f"fixture: {good}/{len(rows)} done with one search from the top field\n")
    return 0 if met else 1


if __name__ == "__main__":
    sys.exit(main())
