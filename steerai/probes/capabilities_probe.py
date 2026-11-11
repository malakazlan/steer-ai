"""Measure the interaction model's perception (packet interaction-model-2026-11-12 §1, §5).

`--section gallery`: the gallery page in a throwaway Chrome; for every element whose name belongs to
a gallery row, print its control type and capability tokens (observe only, no input).
`--section cost`: on each page, n fetches with the old 8 cached properties and n with the full set,
interleaved; print fetch_ms p50/p95 and the node count. Pages are public; nothing typed.

Run: `uv run python -m steerai.probes.capabilities_probe --section gallery|cost [--runs 5]`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from steerai.probes import uia_snapshot
from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome, page_ws_url, stop_chrome
from steerai.probes.element_list import elements_from_nodes
from steerai.probes.gallery_page import page_html
from steerai.probes.layer_b import WINDOW, _free_port
from steerai.probes.layer_b_pages import PageServer
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.windows import wait_window_for_pid

GALLERY_NAMES = (
    "Save draft",
    "Bold",
    "Dark mode",
    "Email me updates",
    "Remember this device",
    "Select all files",
    "Standard",
    "Express",
    "A4",
    "Letter",
    "Profile",
    "Billing details",
    "Favourite fruit",
    "Apple",
    "Mango",
    "Shelf",
    "Fiction",
    "Country",
    "Time zone",
    "Colour",
    "Shipping policy",
    "Show filters",
    "Share",
    "Copy link",
    "Show word count",
    "Small font",
    "Large font",
    "Folders",
    "Projects",
    "Alpha",
    "Orders",
    "Order 1001",
    "Full name",
    "Find articles",
    "Comments",
    "Terms of service",
    "Volume",
    "Quantity",
)
COST_PAGES = (
    "https://en.wikipedia.org/wiki/Ada_Lovelace",
    "https://www.youtube.com/",
    "https://github.com/python/cpython",
)
OLD_PROPERTIES = uia_snapshot._CACHED_PROPERTIES[:8]


def _out(row: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(row) + "\n")
    sys.stdout.flush()


def _open(url: str) -> tuple[subprocess.Popen[bytes], Path, int]:
    tmp = Path(tempfile.mkdtemp(prefix="steer-caps-"))
    port = _free_port()
    proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url, window_size=WINDOW)
    window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
    page_ws_url(port)
    time.sleep(4.0)
    return proc, tmp, window.hwnd


def gallery() -> None:
    with PageServer({"/page.html": page_html()}) as server:
        proc, tmp, hwnd = _open(f"http://127.0.0.1:{server.port}/page.html")
        try:
            tree = UiaClient().cached_tree(hwnd)
            for element in elements_from_nodes(tree.nodes):
                if element.name.strip() in GALLERY_NAMES:
                    _out({"name": element.name.strip(), "role": element.role, "caps": element.caps})
            _out({"fetch_ms": round(tree.fetch_ms, 1), "nodes": len(tree.nodes)})
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


def cost(runs: int) -> None:
    full = uia_snapshot._CACHED_PROPERTIES
    for url in COST_PAGES:
        proc, tmp, hwnd = _open(url)
        try:
            client = UiaClient()
            timings: dict[str, list[float]] = {"old": [], "full": []}
            nodes = 0
            for _ in range(runs):
                for label, props in (("old", OLD_PROPERTIES), ("full", full)):
                    tree = client.cached_tree(hwnd, props)
                    timings[label].append(tree.fetch_ms)
                    nodes = len(tree.nodes)
            row: dict[str, object] = {"url": url, "nodes": nodes}
            for label, values in timings.items():
                row[f"{label}_p50"] = round(statistics.median(values), 1)
                row[f"{label}_max"] = round(max(values), 1)
            _out(row)
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--section", choices=("gallery", "cost"), required=True)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    if args.section == "gallery":
        gallery()
    else:
        cost(args.runs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
