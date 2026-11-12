"""What Chrome does when Steer drives each new row's pattern (packet interaction-model-2026-11-12
§2; principal item 4: TreeItem, menuitemcheckbox and menuitemradio stay refused until measured).

For each act: a fresh gallery page in a throwaway Chrome, the target found by role and name in a
fresh snapshot, the pattern method called through UIA (Toggle, SelectionItem.Select,
ExpandCollapse.Expand), then the element's state re-read live and the page's own log read over
CDP: whether the page saw a trusted event, and what. Observe-and-act on a local fixture only.

Run: `uv run python -m steerai.probes.gallery_acts --runs 3`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome, page_ws_url, stop_chrome
from steerai.probes.element_list import elements_from_nodes
from steerai.probes.gallery_page import LOG_KEY, page_html
from steerai.probes.layer_b import WINDOW, _free_port
from steerai.probes.layer_b_pages import PageServer
from steerai.probes.task_suite import _read
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.win_input import acquire_foreground, click_at
from steerai.probes.windows import wait_window_for_pid

ACTS: tuple[tuple[str, str, str, str, tuple[tuple[str, str, str], ...]], ...] = (
    # (label, role, name, act, setup acts run first as (role, name, act))
    ("switch toggle", "Button", "Dark mode", "toggle", ()),
    ("aria-pressed toggle", "Button", "Bold", "toggle", ()),
    ("tri-state toggle", "CheckBox", "Select all files", "toggle", ()),
    ("grid row select", "DataItem", "Order 1001", "select", ()),
    ("tree expand", "TreeItem", "Projects", "expand", ()),
    ("tree child select", "TreeItem", "Alpha", "select", (("TreeItem", "Projects", "expand"),)),
    ("menu open", "Button", "Share", "expand", ()),
    (
        "menuitemcheckbox toggle",
        "MenuItem",
        "Show word count",
        "toggle",
        (("Button", "Share", "expand"),),
    ),
    (
        "menuitemradio select",
        "MenuItem",
        "Large font",
        "select",
        (("Button", "Share", "expand"),),
    ),
    ("menuitem invoke", "MenuItem", "Copy link", "invoke", (("Button", "Share", "expand"),)),
    ("tree label click", "TreeItem", "Projects", "click", ()),
    (
        "tree label click twice",
        "TreeItem",
        "Projects",
        "click",
        (("TreeItem", "Projects", "click"),),
    ),
    ("tree child click", "TreeItem", "Alpha", "click", (("TreeItem", "Projects", "click"),)),
)
"""`click` is a physical click on the item's own label text (as `executor.click_point` picks it),
the way a person opens a tree item: Chrome ignores Expand on one ([v2.77])."""


def _out(row: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(row) + "\n")
    sys.stdout.flush()


def _find(client: UiaClient, hwnd: int, role: str, name: str) -> tuple[Any, list[str]]:
    """The cached handle of every element with this role and name (Any: comtypes)."""
    tree = client.cached_tree(hwnd)
    found = [
        (handle, element)
        for handle, element in zip(tree.handles, elements_from_nodes(tree.nodes), strict=True)
        if element.role == role and element.name.strip() == name
    ]
    return [h for h, _ in found], [str(list(e.caps)) for _, e in found]


def _click_label(client: UiaClient, hwnd: int, role: str, name: str) -> str:
    """A physical click on the centre of the item's first Text descendant named like it."""
    elements = elements_from_nodes(client.cached_tree(hwnd).nodes)
    items = [e for e in elements if e.role == role and e.name.strip() == name]
    if len(items) != 1:
        return "not_found"
    by_id = {e.id: e for e in elements}

    def under(element_id: int) -> bool:
        parent = by_id[element_id].parent_id
        while parent is not None:
            if parent == items[0].id:
                return True
            parent = by_id[parent].parent_id
        return False

    labels = [
        e
        for e in elements
        if e.role == "Text" and e.name.strip() == name and e.has_area and under(e.id)
    ]
    box = labels[0].box if labels else items[0].box
    acquire_foreground(hwnd)
    click_at((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
    return "clicked_label" if labels else "clicked_item"


def _act(client: UiaClient, handle: Any, act: str) -> str:  # Any: a comtypes element
    mod = client._uia_module
    calls: dict[str, tuple[str, str, Callable[[Any], None]]] = {
        "toggle": ("UIA_TogglePatternId", "IUIAutomationTogglePattern", lambda p: p.Toggle()),
        "select": (
            "UIA_SelectionItemPatternId",
            "IUIAutomationSelectionItemPattern",
            lambda p: p.Select(),
        ),
        "expand": (
            "UIA_ExpandCollapsePatternId",
            "IUIAutomationExpandCollapsePattern",
            lambda p: p.Expand(),
        ),
        "invoke": ("UIA_InvokePatternId", "IUIAutomationInvokePattern", lambda p: p.Invoke()),
    }
    pattern_id, interface, call = calls[act]
    raw = handle.GetCurrentPattern(getattr(mod, pattern_id))
    if not raw:
        return "no_pattern"
    call(raw.QueryInterface(getattr(mod, interface)))
    return "called"


def run(runs: int, only: str = "") -> None:
    for label, role, name, act, setup in ACTS:
        if only and only not in label:
            continue
        for number in range(1, runs + 1):
            tmp = Path(tempfile.mkdtemp(prefix="steer-acts-"))
            with PageServer({"/page.html": page_html()}) as server:
                port = _free_port()
                proc = launch_chrome(
                    CHROME_DEFAULT,
                    tmp / "profile",
                    port,
                    f"http://127.0.0.1:{server.port}/page.html",
                    window_size=WINDOW,
                )
                try:
                    window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
                    page_ws_url(port)
                    client = UiaClient()
                    deadline = time.monotonic() + 10.0
                    while (
                        time.monotonic() < deadline
                        and len(client.cached_tree(window.hwnd).nodes) < 90
                    ):
                        time.sleep(0.25)
                    for s_role, s_name, s_act in setup:
                        handles, _ = _find(client, window.hwnd, s_role, s_name)
                        if s_act == "click":
                            _click_label(client, window.hwnd, s_role, s_name)
                        elif len(handles) == 1:
                            _act(client, handles[0], s_act)
                        time.sleep(0.4)
                    handles, before = _find(client, window.hwnd, role, name)
                    if act == "click":
                        outcome = _click_label(client, window.hwnd, role, name)
                    else:
                        outcome = _act(client, handles[0], act) if handles else "not_found"
                    time.sleep(0.6)
                    _, after = _find(client, window.hwnd, role, name)
                    log = _read(port, f'localStorage.getItem("{LOG_KEY}") || "[]"', "log")
                    _out(
                        {
                            "act": label,
                            "run": number,
                            "matches": len(handles),
                            "outcome": outcome,
                            "caps_before": before,
                            "caps_after": after,
                            "log": json.loads(log),
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - a probe reports any COM failure as data
                    _out(
                        {"act": label, "run": number, "error": f"{type(exc).__name__}: {exc}"[:160]}
                    )
                finally:
                    stop_chrome(proc, tmp / "profile")
                    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--only", default="", help="run only the acts whose label holds this")
    args = parser.parse_args()
    run(args.runs, args.only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
