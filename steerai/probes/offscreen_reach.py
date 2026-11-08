"""Probe for offscreen elements in the planner's lines (H2 l1, #151; 2026-11-08): for elements
below the fold, in a scrolled list and in a hidden panel, what Steer's world model reports
(offscreen flag, box), whether the element exposes ScrollItemPattern, whether ScrollIntoView
brings it on screen, and how many planner lines the page has with and without offscreen elements.
A local fixture page, throwaway profile, semantic UIA only (no OS input; the window is brought in
front behind the idle gate).

Usage (Windows):
    uv run python -m steerai.probes.offscreen_reach --runs 5
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome, page_ws_url, stop_chrome
from steerai.probes.layer_b import WINDOW, _free_port, _in_front, wait_until_idle
from steerai.probes.layer_b_pages import PageServer

OPTIONS = 30
PAGE = (
    "<!doctype html><title>Offscreen fixture</title>"
    "<h1>Feed</h1><p>Top of the page.</p>"
    '<ul role="listbox" aria-label="Choices" style="height:120px;overflow:auto">'
    + "".join(f'<li role="option" tabindex="-1">Choice {i}</li>' for i in range(1, OPTIONS + 1))
    + "</ul>"
    '<div style="height:2500px"></div>'
    '<button id="more">Load more</button>'
    '<div hidden><button id="ghost">Hidden action</button></div>'
    '<nav style="position:fixed;top:0;right:0;width:300px;transform:translateX(100%)">'
    '<button id="drawer">Drawer action</button></nav>'
    '<span style="position:absolute;left:-9999px">Screen reader note</span>'
    '<div inert><button id="inert">Inert action</button></div>'
)
TARGETS = (
    ("below_fold", "Button", "Load more"),
    ("in_list", "ListItem", f"Choice {OPTIONS}"),
    ("hidden", "Button", "Hidden action"),
    ("drawer", "Button", "Drawer action"),
    ("sr_only", "Text", "Screen reader note"),
    ("inert", "Button", "Inert action"),
)
SCROLL_ITEM = 10017


def _measure(number: int) -> list[dict[str, Any]]:
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415
    from steerai.run import warm_up  # noqa: PLC0415
    from steerai.world.elements import prune  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    wait_until_idle()
    tmp = Path(tempfile.mkdtemp(prefix="steer-offscreen-"))
    rows: list[dict[str, Any]] = []
    with PageServer({"/page.html": PAGE}) as server:
        port = _free_port()
        url = f"http://127.0.0.1:{server.port}/page.html"
        proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url, window_size=WINDOW)
        try:
            window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
            page_ws_url(port)
            _in_front(window.hwnd)
            client = UiaClient()
            model = WorldModel(client)
            snap = warm_up(lambda: model.observe(window.hwnd), cap_s=15.0)
            everything = len(snap.elements)
            pruned = len(prune(snap.elements, cap=max(everything, 1)))
            rows.append(
                {
                    "run": number,
                    "elements": everything,
                    "lines_pruned": pruned,
                    "offscreen": sum(1 for e in snap.elements if e.offscreen),
                }
            )
            for case, role, name in TARGETS:
                found = [e for e in snap.elements if e.role == role and e.name == name]
                row: dict[str, Any] = {"run": number, "case": case, "in_tree": len(found)}
                if len(found) == 1:
                    element = found[0]
                    handle = snap.handle(element.id)
                    row["offscreen"] = element.offscreen
                    row["box"] = element.box
                    root = snap.elements[0].box
                    row["overlaps_horizontally"] = (
                        element.box[2] > root[0] and element.box[0] < root[2]
                    )
                    pattern = handle.GetCurrentPattern(SCROLL_ITEM)
                    row["scroll_item"] = bool(pattern)
                    if pattern:
                        started = time.perf_counter()
                        query = client._uia_module.IUIAutomationScrollItemPattern
                        pattern.QueryInterface(query).ScrollIntoView()
                        row["scroll_ms"] = round((time.perf_counter() - started) * 1000, 2)
                        time.sleep(0.5)
                        row["offscreen_after"] = bool(handle.CurrentIsOffscreen)
                rows.append(row)
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offscreen elements and ScrollIntoView.")
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    for number in range(1, args.runs + 1):
        for row in _measure(number):
            sys.stdout.write(json.dumps(row) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
