"""V2 step 2b probe ([V2.2] §6.4 "not produced in 2a"; [V2.4] the query classifier moved into
step 3c, probe first): what Chrome's UIA tree exposes, on a local page, for

- LandmarkType and LocalizedLandmarkType on a search landmark: `role=search`, `<search>`, a
  `<form role=search>`, and their fields (`<input type=search>`, `role=searchbox`, plain text);
- AriaRole and AriaProperties on the same fields and on a heading, a dialog and a combobox;
- HeadingLevel on `<h1>`-`<h3>` (UIA has no heading control type);
- HasKeyboardFocus on a field before and after DevTools focuses it;
- the Value pattern's value on a field before and after DevTools types into it.

Measured 2026-11-14, Chrome 157.0.8103.51, n=1:
- `role=search`, `<search>` and `<form role=search>`: LandmarkType 80004 (Search),
  LocalizedLandmarkType "search", AriaRole "search". Fields carry LandmarkType 0.
- `<input type=search>` and `role=searchbox`: AriaRole "searchbox"; a text input (also inside
  `<search>`): "textbox"; `role=combobox`: "combobox" with `haspopup=listbox`.
- `<h1>`-`<h3>`: ControlType Text, AriaRole "heading", HeadingLevel 80051-80053 (level 1-3);
  everything else HeadingLevel 80050 (none). The heading carrier is readable after all.
- `role=dialog`: ControlType Window, AriaRole "dialog".
- A text field focused by DevTools: HasKeyboardFocus true; after typing, the Value pattern reads
  the typed text.

One throwaway Chrome profile; changes are made through DevTools, never OS input. Each element is
found by its automation id (the HTML `id`). Prints closed values and made-up page text only.

Usage (Windows, session unlocked):
    uv run python -m steerai.probes.uia_props --runs 3
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

from steerai.probes.chrome_a11y import (
    CHROME_DEFAULT,
    CdpSession,
    launch_chrome,
    page_ws_url,
    stop_chrome,
)
from steerai.probes.layer_b import _free_port
from steerai.probes.layer_b_pages import PageServer

PAGE = """<!doctype html><meta charset="utf-8"><title>Props</title>
<div role="search" id="land-role" aria-label="Site search">
  <input type="search" id="f-search" aria-label="Search the site">
  <button id="b-role">Go</button>
</div>
<search id="land-el"><input type="text" id="f-text-in-search" aria-label="Find"></search>
<form role="search" id="land-form" aria-label="Catalogue search">
  <input role="searchbox" id="f-searchbox" aria-label="Catalogue">
  <button id="b-form" type="submit">Search</button>
</form>
<input type="text" id="f-plain" aria-label="Plain field">
<input role="combobox" id="f-combo" aria-label="City" aria-expanded="false">
<h1 id="h-1">Heading one</h1><h2 id="h-2">Heading two</h2><h3 id="h-3">Heading three</h3>
<div role="dialog" id="dlg" aria-label="Settings dialog"><button id="dlg-ok">OK</button></div>
<p id="ready">Props ready</p>
"""

IDS = (
    "land-role",
    "land-el",
    "land-form",
    "f-search",
    "f-text-in-search",
    "f-searchbox",
    "f-plain",
    "f-combo",
    "b-role",
    "b-form",
    "h-1",
    "h-2",
    "h-3",
    "dlg",
)
FOCUS_FIELD = "f-plain"
TYPED = "red shoes"
_IDS = {
    "LandmarkType": 30157,
    "LocalizedLandmarkType": 30158,
    "AriaRole": 30101,
    "AriaProperties": 30102,
    "HeadingLevel": 30173,
    "HasKeyboardFocus": 30008,
    "ControlType": 30003,
}


def _uia() -> tuple[Any, Any]:  # Any: comtypes UIA objects have no stubs
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only

    client = UiaClient()
    return client._uia, client._uia_module


def _find(uia: Any, mod: Any, hwnd: int, automation_id: str) -> Any:  # Any: comtypes element
    root = uia.ElementFromHandle(hwnd)
    condition = uia.CreatePropertyCondition(mod.UIA_AutomationIdPropertyId, automation_id)
    return root.FindFirst(mod.TreeScope_Descendants, condition)


def _props(mod: Any, element: Any) -> dict[str, Any]:  # Any: comtypes values
    if not element:
        return {"found": False}
    out: dict[str, Any] = {"found": True}
    for name, prop in _IDS.items():
        try:
            value = element.GetCurrentPropertyValue(prop)
        except OSError as exc:
            out[name] = f"error:{type(exc).__name__}"
            continue
        out[name] = value if isinstance(value, (int, str, bool)) or value is None else str(value)
    pattern = element.GetCurrentPattern(mod.UIA_ValuePatternId)
    out["Value"] = (
        str(pattern.QueryInterface(mod.IUIAutomationValuePattern).CurrentValue) if pattern else None
    )
    return out


def run_once() -> dict[str, Any]:
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415 - Windows only
    from steerai.run import warm_up  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="steer-props-"))
    with PageServer({"/props": PAGE}) as server:
        port = _free_port()
        proc = launch_chrome(
            CHROME_DEFAULT, tmp / "profile", port, f"http://127.0.0.1:{server.port}/props"
        )
        try:
            hwnd = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc).hwnd
            cdp = CdpSession(page_ws_url(port))
            model = WorldModel(UiaClient())
            warm_up(lambda: model.observe(hwnd), cap_s=15.0)
            uia, mod = _uia()
            deadline = time.monotonic() + 20.0
            while not _find(uia, mod, hwnd, "f-plain") and time.monotonic() < deadline:
                time.sleep(0.5)
            before = {i: _props(mod, _find(uia, mod, hwnd, i)) for i in IDS}
            cdp.call(
                "Runtime.evaluate",
                {"expression": f"document.getElementById('{FOCUS_FIELD}').focus()"},
            )
            cdp.call("Input.insertText", {"text": TYPED})
            time.sleep(1.0)
            after = _props(mod, _find(uia, mod, hwnd, FOCUS_FIELD))
            cdp.close()
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)
    return {"elements": before, "focused_after": after, "typed": TYPED}


SEARCH_LANDMARK = 80004
"""UIA_SearchLandmarkTypeId."""
FIELD_TYPES = (50004, 50003)
"""Edit and ComboBox: the fields a search Enter is sent from."""


def site_fields(url: str) -> dict[str, Any]:  # Any: JSON values
    """The page's visible Edit and ComboBox fields on `url`: per field its AriaRole and whether an
    ancestor is a search landmark (the §7.2 classifier's two inputs). Read-only: no input."""
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415 - Windows only
    from steerai.run import warm_up  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="steer-sitefields-"))
    port = _free_port()
    proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url)
    try:
        hwnd = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc).hwnd
        page_ws_url(port)
        time.sleep(6.0)  # the site settles its first layout and any redirect
        model = WorldModel(UiaClient())
        warm_up(lambda: model.observe(hwnd), cap_s=15.0)
        uia, mod = _uia()
        root = uia.ElementFromHandle(hwnd)
        documents = root.FindAll(
            mod.TreeScope_Descendants,
            uia.CreatePropertyCondition(
                mod.UIA_ControlTypePropertyId, mod.UIA_DocumentControlTypeId
            ),
        )
        fields = []
        for d in range(documents.Length):
            found = documents.GetElement(d).FindAll(
                mod.TreeScope_Descendants, uia.CreateTrueCondition()
            )
            for i in range(found.Length):
                element = found.GetElement(i)
                if element.CurrentControlType not in FIELD_TYPES or element.CurrentIsOffscreen:
                    continue
                landmark, node = False, element
                for _ in range(40):
                    node = uia.ControlViewWalker.GetParentElement(node)
                    if not node:
                        break
                    if node.GetCurrentPropertyValue(_IDS["LandmarkType"]) == SEARCH_LANDMARK:
                        landmark = True
                        break
                fields.append(
                    {
                        "role": str(element.GetCurrentPropertyValue(_IDS["AriaRole"])),
                        "in_search_landmark": landmark,
                    }
                )
    finally:
        stop_chrome(proc, tmp / "profile")
        shutil.rmtree(tmp, ignore_errors=True)
    query = any(f["role"] == "searchbox" or f["in_search_landmark"] for f in fields)
    return {"fields": fields, "query_class_possible": query}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--sites", action="store_true", help="the pooled set's search sites")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        sys.stdout.write(json.dumps({"skipped": "windows only"}) + "\n")
        return 0
    if args.sites:
        from steerai.probes.task_suite import H2, H3, H4, H5, H6, HELD_OUT, TASKS  # noqa: PLC0415

        seen: set[str] = set()
        for task in (*TASKS, *HELD_OUT, *H2, *H3, *H4, *H5, *H6):
            if not task.start.startswith("https://") or task.start in seen:
                continue
            seen.add(task.start)
            try:
                row = site_fields(task.start)
            except (OSError, TimeoutError, RuntimeError) as exc:
                row = {"error": type(exc).__name__}
            sys.stdout.write(json.dumps({"task": task.task_id, **row}) + "\n")
            sys.stdout.flush()
        return 0
    for number in range(1, args.runs + 1):
        sys.stdout.write(json.dumps({"run": number, **run_once()}, default=str) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
