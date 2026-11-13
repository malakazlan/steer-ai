"""V2 step 2a follow-up (packet v2-step2-effect-observer v3, "Probes" and invariant 3): real
Chrome before/after pairs for the effect observer from local pages, and three platform facts:

- `document`: does the top Document keep its RuntimeId over a pushState, a fragment change and a
  reload, and is it replaced by a real navigation (EO-07)?
- `offscreen`: which ways of hiding a menu item leave it in the UIA tree, and with IsOffscreen
  set (EO-17)?
- `pairs`: one before/after pair per observer rule, with the report the observer gives on it.

Each run uses a fresh throwaway Chrome profile and a page server on 127.0.0.1 that serves the
pages below from memory. Changes are made through DevTools (`Runtime.evaluate`); no OS input is
sent. Pages hold only the made-up text below. Prints closed tokens and counts only; `--out`
writes the pairs as JSON fixtures for `tests/world/test_effects_pages.py`.

Usage (Windows, session unlocked):
    uv run python -m steerai.probes.effect_pages --section document --runs 3
    uv run python -m steerai.probes.effect_pages --section offscreen --runs 3
    uv run python -m steerai.probes.effect_pages --section pairs --runs 1 --out <dir>
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from steerai.probes.chrome_a11y import (
    CHROME_DEFAULT,
    CdpSession,
    launch_chrome,
    page_ws_url,
    stop_chrome,
)
from steerai.probes.layer_b import _free_port
from steerai.probes.layer_b_pages import PageServer
from steerai.world.address_bar import DocumentUrl
from steerai.world.effects import EffectReport, Observed, _top_document, observe
from steerai.world.model import Snapshot, WorldModel, snapshot_to_json
from steerai.world.observe2 import read_observed
from steerai.world.watch import watch

WINDOW = (1100, 800)
WATCH_POLL_S = 0.1
WATCH_SETTLE_S = 0.5
"""Unmeasured starting values for `watch`; this section measures what they should be."""
WATCH_DEADLINE_S = 5.0
KEY = secrets.token_bytes(32)
"""The run's own value-digest key: never stored."""
T = TypeVar("T")
SETTLE_S = 1.0
"""Wait after a change before the after read; `WorldModel.observe` then polls until stable."""
READY_S = 20.0

_HEAD = '<!doctype html><meta charset="utf-8"><title>{title}</title>'

SHOP = (
    _HEAD.format(title="Shop")
    + """
<div role="search"><input id="q" role="combobox" aria-label="Search" aria-expanded="false"
 aria-controls="sugg"><ul id="sugg" role="listbox" aria-label="Suggestions" hidden></ul></div>
<main><h1>Shop</h1><ul id="res" aria-label="Products"><li>Blue shoes</li><li>Green hats</li></ul>
<p id="ready">Shop ready</p></main>
"""
)

RESULTS = (
    _HEAD.format(title="Red shoes - Shop")
    + """
<main><h1>Results</h1><ul aria-label="Products"><li>Red shoes for running</li>
<li>Red shoes for walking</li></ul><p id="ready">Results ready</p></main>
"""
)

TREE = (
    _HEAD.format(title="Files")
    + """
<ul role="tree" aria-label="Files"><li role="treeitem" id="reports" aria-expanded="false"
 tabindex="0"><span>Reports</span><ul role="group" id="kids" hidden><li role="treeitem">January
</li><li role="treeitem">February</li></ul></li></ul><p id="ready">Files ready</p>
"""
)

MENU = (
    _HEAD.format(title="Items")
    + """
<button id="sort" aria-haspopup="menu" aria-expanded="false">Sort</button>
<div id="menu" role="menu" aria-label="Sort order" hidden><div role="menuitem">Newest first
</div><div role="menuitem">Oldest first</div></div>
<main><h1>Items</h1><p>Item one</p><p id="ready">Items ready</p></main>
"""
)

HIDING = (
    _HEAD.format(title="Hiding")
    + """
<style>.m{position:absolute;left:20px;width:200px}</style>
<div role="menu" aria-label="m shown" class="m" style="top:40px">
 <div role="menuitem">item shown</div></div>
<div role="menu" aria-label="m hidden attr" class="m" hidden><div role="menuitem">item hidden
 attr</div></div>
<div role="menu" aria-label="m display none" class="m" style="display:none">
 <div role="menuitem">item display none</div></div>
<div role="menu" aria-label="m visibility hidden" class="m" style="visibility:hidden;top:80px">
 <div role="menuitem">item visibility hidden</div></div>
<div role="menu" aria-label="m left far" class="m" style="left:-9999px;top:120px">
 <div role="menuitem">item left far</div></div>
<div role="menu" aria-label="m zero height" class="m" style="height:0;overflow:hidden;top:160px">
 <div role="menuitem">item zero height</div></div>
<div role="menu" aria-label="m aria hidden" class="m" aria-hidden="true" style="top:200px">
 <div role="menuitem">item aria hidden</div></div>
<div role="menu" aria-label="m below fold" class="m" style="top:3000px">
 <div role="menuitem">item below fold</div></div>
<details style="position:absolute;top:240px;left:20px"><summary>More</summary>
 <div role="menu" aria-label="m details closed"><div role="menuitem">item details closed</div>
 </div></details>
<p id="ready" style="position:absolute;top:280px;left:20px">Hiding ready</p>
"""
)

PAGES = {
    "/shop": SHOP,
    "/results": RESULTS,
    "/tree": TREE,
    "/menu": MENU,
    "/hiding": HIDING,
    "/other": _HEAD.format(title="Other") + '<p id="ready">Other ready</p>',
}

HIDING_WAYS = (
    "shown",
    "hidden attr",
    "display none",
    "visibility hidden",
    "left far",
    "zero height",
    "aria hidden",
    "below fold",
    "details closed",
)


@dataclass(frozen=True, slots=True)
class Pair:
    """One observer rule as a real change: the page, the DevTools script run before the before
    read (`setup`), the change itself (`act`), the acted element as (role, name) and the text."""

    case: str
    path: str
    act: str
    setup: str = ""
    acted: tuple[str, str] | None = None
    text: str | None = None
    ready: str = ""


_SUGGEST = (
    "q.value='red shoes';sugg.innerHTML='<li role=\"option\">red shoes size 9</li>';"
    "sugg.hidden=false;q.setAttribute('aria-expanded','true');"
)

PAIRS = (
    Pair(
        "tree_expand",
        "/tree",
        "reports.setAttribute('aria-expanded','true');kids.hidden=false",
        acted=("TreeItem", "Reports"),
    ),
    Pair(
        "menu_under_button",
        "/menu",
        "sort.setAttribute('aria-expanded','true');menu.hidden=false",
        acted=("Button", "Sort"),
    ),
    Pair(
        "instant_results",
        "/shop",
        _SUGGEST + "res.innerHTML='<li>Red shoes for running</li><li>Red shoes for walking</li>'",
        acted=("ComboBox", "Search"),
        text="red shoes",
    ),
    Pair(
        "dropdown_close",
        "/shop",
        "sugg.hidden=true;q.setAttribute('aria-expanded','false')",
        setup=_SUGGEST,
        acted=("ComboBox", "Search"),
        text="red shoes",
    ),
    Pair(
        "results_page",
        "/shop",
        "location.href='/results?q=red+shoes'",
        acted=("ComboBox", "Search"),
        text="red shoes",
        ready="Results ready",
    ),
    Pair(
        "push_state",
        "/shop",
        "history.pushState({},'','/shop?q=red+shoes')",
        acted=("ComboBox", "Search"),
        text="red shoes",
    ),
    Pair("enter_nothing", "/shop", "void 0", acted=("ComboBox", "Search"), text="red shoes"),
    Pair("rerender", "/shop", "res.innerHTML=res.innerHTML", acted=("ComboBox", "Search")),
)


class _TimedReader:
    """The browser's UIA client as a `FactReader`, each read timed under its own name."""

    def __init__(self, browser: _Browser) -> None:
        self._browser = browser

    def document_url(self, hwnd: int) -> DocumentUrl | None:
        client = self._browser.client
        return self._browser.timed("url", lambda: client.document_url(hwnd))

    def focused_runtime_id(self) -> tuple[int, ...]:
        return self._browser.timed("focus", self._browser.client.focused_runtime_id)

    def roles_and_headings(
        self, hwnd: int
    ) -> tuple[dict[tuple[int, ...], str], dict[tuple[int, ...], int]]:
        client = self._browser.client
        return self._browser.timed("roles", lambda: client.roles_and_headings(hwnd))


class _Browser:
    """One throwaway Chrome on one local page, driven through DevTools."""

    def __init__(self, url: str) -> None:
        from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
        from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415 - Windows only

        self._tmp = Path(tempfile.mkdtemp(prefix="steer-effects-"))
        port = _free_port()
        self._proc = launch_chrome(
            CHROME_DEFAULT, self._tmp / "profile", port, url, window_size=WINDOW
        )
        try:
            self.hwnd = wait_window_for_pid(
                self._proc.pid, image="chrome.exe", process=self._proc
            ).hwnd
            self._cdp = CdpSession(page_ws_url(port))
        except BaseException:
            self.close()
            raise
        self.client = UiaClient()
        self._model = WorldModel(self.client)
        self.costs: dict[str, list[float]] = {}
        from steerai.run import warm_up  # noqa: PLC0415 - the probe's only use

        warm_up(lambda: self._model.observe(self.hwnd), cap_s=15.0)

    def run(self, script: str) -> Any:  # Any: the JSON value the page script returns
        reply = self._cdp.call(
            "Runtime.evaluate", {"expression": script, "returnByValue": True, "awaitPromise": True}
        )
        if "exceptionDetails" in reply:
            raise RuntimeError("page script failed")
        return reply.get("result", {}).get("value")

    def windows(self) -> tuple[int, ...]:
        from steerai.probes.windows import list_top_windows  # noqa: PLC0415 - Windows only

        return tuple(sorted(w.hwnd for w in list_top_windows() if w.pid == self._proc.pid))

    def read(self) -> Observed:
        snapshot = self._model.observe(self.hwnd, cap_s=5.0)
        return Observed(
            snapshot=snapshot, url=str(self.run("location.href")), windows=self.windows()
        )

    def timed(self, name: str, read: Callable[[], T]) -> T:
        started = time.perf_counter()
        try:
            return read()
        finally:
            self.costs.setdefault(name, []).append((time.perf_counter() - started) * 1000)

    def read_v2(self) -> Observed:
        """The v2 observe path (`read_observed`) on one snapshot fetch, each read timed."""
        snapshot = self.timed(
            "snapshot", lambda: self._model.observe(self.hwnd, cap_s=1.0, stable_window=1)
        )
        return read_observed(snapshot, _TimedReader(self), key=KEY, windows=self.windows)

    def ready(self, marker: str) -> Observed:
        deadline = time.monotonic() + READY_S
        while True:
            seen = self.read()
            if any(e.name == marker for e in seen.snapshot.elements):
                return seen
            if time.monotonic() > deadline:
                roles = sorted({e.role for e in seen.snapshot.elements})
                raise TimeoutError(
                    f"no ready marker; {len(seen.snapshot.elements)} nodes, roles {roles}"
                )
            time.sleep(0.5)

    def close(self) -> None:
        if hasattr(self, "_cdp"):
            self._cdp.close()
        stop_chrome(self._proc, self._tmp / "profile")
        shutil.rmtree(self._tmp, ignore_errors=True)


def _marker(path: str) -> str:
    return {
        "/shop": "Shop ready",
        "/results": "Results ready",
        "/tree": "Files ready",
        "/menu": "Items ready",
        "/hiding": "Hiding ready",
        "/other": "Other ready",
    }[path]


def _rid(snapshot: Snapshot, role: str, name: str) -> tuple[int, ...] | None:
    found = [e.runtime_id for e in snapshot.elements if e.role == role and e.name == name]
    return found[0] if len(found) == 1 else None


def _report_dict(report: EffectReport) -> dict[str, Any]:
    return {
        "classes": sorted(report.classes),
        "tokens": sorted(report.tokens),
        "unread": sorted(report.unread),
        "primary": report.primary,
        "carriers": sorted(report.carriers),
        "revealed_root": report.revealed_root,
        "stable_after": report.stable_after,
    }


def _diff_roles(before: Snapshot, after: Snapshot) -> dict[str, list[str]]:
    """Closed role names only: what appeared (new RuntimeId) and what changed on a kept one."""
    old = {(e.runtime_id, e.role): e for e in before.elements if e.runtime_id}
    new = sorted({e.role for e in after.elements if (e.runtime_id, e.role) not in old})
    changed = set()
    for e in after.elements:
        was = old.get((e.runtime_id, e.role))
        if was is None:
            continue
        for field in ("name", "enabled", "offscreen", "caps"):
            if getattr(was, field) != getattr(e, field):
                changed.add(f"{e.role}.{field}")
    return {"appeared": new, "changed": sorted(changed)}


# ---- sections ----------------------------------------------------------------------------------


DOCUMENT_CHANGES = (
    ("push_state", "history.pushState({},'','/shop?q=x')", "Shop ready"),
    ("fragment", "location.hash='part'", "Shop ready"),
    ("reload", "location.reload()", "Shop ready"),
    ("navigate", "location.href='/other'", "Other ready"),
    ("navigate_query", "location.href='/results?q=red+shoes'", "Results ready"),
)


def document_run(base: str) -> list[dict[str, Any]]:
    rows = []
    for change, script, marker in DOCUMENT_CHANGES:
        browser = _Browser(base + "/shop")
        try:
            before = browser.ready("Shop ready")
            browser.run(script)
            time.sleep(SETTLE_S)
            after = browser.ready(marker)
            top_b, top_a = _top_document(before.snapshot), _top_document(after.snapshot)
            rows.append(
                {
                    "change": change,
                    "top_before": top_b is not None,
                    "top_after": top_a is not None,
                    "same_rid": None
                    if top_b is None or top_a is None
                    else top_b.runtime_id == top_a.runtime_id,
                    "url_changed": before.url != after.url,
                    "classes": sorted(observe(before, after).classes),
                    **_diff_roles(before.snapshot, after.snapshot),
                }
            )
        finally:
            browser.close()
    return rows


def offscreen_run(base: str) -> list[dict[str, Any]]:
    browser = _Browser(base + "/hiding")
    try:
        seen = browser.ready("Hiding ready").snapshot
    finally:
        browser.close()
    rows = []
    for way in HIDING_WAYS:
        items = [e for e in seen.elements if e.role == "MenuItem" and e.name == f"item {way}"]
        menus = [e for e in seen.elements if e.role == "Menu" and e.name == f"m {way}"]
        rows.append(
            {
                "way": way,
                "item_in_tree": bool(items),
                "item_offscreen": items[0].offscreen if items else None,
                "item_has_area": items[0].has_area if items else None,
                "menu_in_tree": bool(menus),
                "menu_offscreen": menus[0].offscreen if menus else None,
            }
        )
    return rows


def pair_run(base: str, pair: Pair, out: Path | None, number: int) -> dict[str, Any]:
    browser = _Browser(base + pair.path)
    try:
        browser.ready(_marker(pair.path))
        if pair.setup:
            browser.run(pair.setup)
            time.sleep(SETTLE_S)
        before = browser.read()
        acted = _rid(before.snapshot, *pair.acted) if pair.acted else None
        browser.run(pair.act)
        time.sleep(SETTLE_S)
        after = browser.ready(pair.ready) if pair.ready else browser.read()
    finally:
        browser.close()
    report = observe(before, after, acted=acted, text=pair.text)
    row = {
        "case": pair.case,
        "run": number,
        "acted_found": acted is not None,
        "report": _report_dict(report),
        **_diff_roles(before.snapshot, after.snapshot),
    }
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        fixture = {
            "case": pair.case,
            "before": json.loads(snapshot_to_json(before.snapshot)),
            "after": json.loads(snapshot_to_json(after.snapshot)),
            "before_url": before.url,
            "after_url": after.url,
            "windows_before": list(before.windows or ()),
            "windows_after": list(after.windows or ()),
            "acted": list(acted) if acted is not None else None,
            "text": pair.text,
            "report": row["report"],
        }
        (out / f"{pair.case}.json").write_text(json.dumps(fixture, indent=1), encoding="utf-8")
    return row


def _emit(row: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(row) + "\n")
    sys.stdout.flush()


def watch_run(base: str, pair: Pair) -> dict[str, Any]:
    """One pair through `observer.watch` on the v2 observe path: the measured first change,
    settle and per-read costs ([V2.4]: the per-step URL cost is reported in 3b)."""
    browser = _Browser(base + pair.path)
    try:
        browser.ready(_marker(pair.path))
        if pair.setup:
            browser.run(pair.setup)
            time.sleep(SETTLE_S)
        probe = browser.read()
        acted = _rid(probe.snapshot, *pair.acted) if pair.acted else None
        browser.costs.clear()
        before = browser.read_v2()
        browser.run(pair.act)
        result = watch(
            browser.read_v2,
            before,
            acted=acted,
            text=pair.text,
            clock=time.monotonic,
            sleep=time.sleep,
            poll_s=WATCH_POLL_S,
            settle_s=WATCH_SETTLE_S,
            deadline_s=WATCH_DEADLINE_S,
        )
        costs = {k: _pcts(v) for k, v in browser.costs.items()}
    finally:
        browser.close()
    return {
        "case": pair.case,
        "first_change_ms": result.first_change_ms,
        "settled_ms": result.settled_ms,
        "timed_out": result.timed_out,
        "report": _report_dict(result.report),
        "read_ms": costs,
        "url_read": [before.url is not None, result.after.url is not None],
        "url_changed": before.url != result.after.url,
    }


def _pcts(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50": round(ordered[len(ordered) // 2], 1),
        "max": round(ordered[-1], 1),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--section", choices=("document", "offscreen", "pairs", "watch"), required=True
    )
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--case", action="append", default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        _emit({"skipped": "windows only"})
        return 0
    with PageServer(PAGES) as server:
        base = f"http://127.0.0.1:{server.port}"
        for number in range(1, args.runs + 1):
            if args.section == "document":
                for row in document_run(base):
                    _emit({"run": number, **row})
            elif args.section == "offscreen":
                for row in offscreen_run(base):
                    _emit({"run": number, **row})
            elif args.section == "watch":
                for pair in PAIRS:
                    if args.case and pair.case not in args.case:
                        continue
                    _emit({"run": number, **watch_run(base, pair)})
            else:
                for pair in PAIRS:
                    if args.case and pair.case not in args.case:
                        continue
                    _emit(pair_run(base, pair, args.out, number))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
