"""Probe for the widget-verb design (H2 classes A-D, 2026-11-08): which UIA control patterns Chrome
exposes on native and ARIA widgets, whether the semantic action changes the page's own state and
reaches its event handlers, and whether the UIA state reads back the change. A local fixture
page (never the spent H2 page), throwaway profile, semantic UIA calls only (no OS input; the
window is brought in front behind the idle gate). n runs per widget.

Widgets: a native checkbox (Toggle), an ARIA tablist (`role=tab` with aria-selected,
SelectionItem.Select), native radio buttons (SelectionItem.Select), a native select
(ExpandCollapse, then SelectionItem.Select on the option, then Collapse).

Usage (Windows):
    uv run python -m steerai.probes.widget_patterns --runs 5
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome, page_ws_url, stop_chrome
from steerai.probes.layer_b import WINDOW, _evaluate, _free_port, _in_front, wait_until_idle
from steerai.probes.layer_b_pages import PageServer

PAGE = """<!doctype html><title>Widget fixture</title>
<label><input type="checkbox" id="cb"> Email alerts</label>
<label>Size <select id="sel">
  <option>Small</option><option>Medium</option><option>Large</option>
</select></label>
<div role="tablist" aria-label="Sections">
  <button role="tab" id="tab-a" aria-selected="true">Overview</button>
  <button role="tab" id="tab-b" aria-selected="false">Details</button>
</div>
<div id="panel" role="tabpanel">Overview text</div>
<fieldset><legend>Plan</legend>
  <label><input type="radio" name="plan" id="r1" checked> Basic</label>
  <label><input type="radio" name="plan" id="r2"> Pro</label>
</fieldset>
<script>
window.LOG = [];
function log(id, type, e) { window.LOG.push(id + ":" + type + (e.isTrusted ? "" : ":untrusted")); }
for (const id of ["cb", "sel", "r1", "r2"]) {
  const el = document.getElementById(id);
  for (const t of ["click", "input", "change"]) el.addEventListener(t, (e) => log(id, t, e));
}
for (const tab of document.querySelectorAll("[role=tab]")) {
  tab.addEventListener("click", (e) => {
    log(tab.id, "click", e);
    for (const t of document.querySelectorAll("[role=tab]")) {
      t.setAttribute("aria-selected", t === tab);
    }
    document.getElementById("panel").textContent = tab.textContent + " text";
  });
}
</script>"""
STATE = (
    "JSON.stringify({cb: document.getElementById('cb').checked,"
    " sel: document.getElementById('sel').value,"
    " tab: document.getElementById('tab-b').getAttribute('aria-selected'),"
    " r2: document.getElementById('r2').checked, log: window.LOG})"
)
PATTERNS = {
    "Invoke": 10000,
    "Selection": 10001,
    "Value": 10002,
    "ExpandCollapse": 10005,
    "SelectionItem": 10010,
    "Toggle": 10015,
    "LegacyIAccessible": 10018,
}
TOGGLE, SELECTION_ITEM, EXPAND_COLLAPSE, VALUE = 10015, 10010, 10005, 10002
OPEN_S = 0.5
"""The probe's own wait after Expand before reading the options, and again before Collapse."""


@dataclass(frozen=True, slots=True)
class Widget:
    name: str
    control_type: str
    """The UIA control type id attribute name, e.g. `UIA_CheckBoxControlTypeId`."""
    label: str
    act: Callable[[Any, Any, Any, int], str]  # Any: comtypes uia, module, element
    read: Callable[[Any, Any], object]  # Any: comtypes module, element


def _pattern(element: Any, mod: Any, pid: int, iface: str) -> Any:  # Any: comtypes
    return element.GetCurrentPattern(pid).QueryInterface(getattr(mod, iface))


def _find(uia: Any, mod: Any, hwnd: int, control_type: int, name: str) -> Any:  # Any: comtypes
    cond = uia.CreateAndCondition(
        uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId, control_type),
        uia.CreatePropertyCondition(mod.UIA_NamePropertyId, name),
    )
    return uia.ElementFromHandle(hwnd).FindFirst(mod.TreeScope_Descendants, cond)


def _toggle(_uia: Any, mod: Any, element: Any, _hwnd: int) -> str:  # Any: comtypes
    _pattern(element, mod, TOGGLE, "IUIAutomationTogglePattern").Toggle()
    return "Toggle"


def _select(_uia: Any, mod: Any, element: Any, _hwnd: int) -> str:  # Any: comtypes
    _pattern(element, mod, SELECTION_ITEM, "IUIAutomationSelectionItemPattern").Select()
    return "SelectionItem.Select"


def _choose_large(uia: Any, mod: Any, element: Any, hwnd: int) -> str:  # Any: comtypes
    expand = _pattern(element, mod, EXPAND_COLLAPSE, "IUIAutomationExpandCollapsePattern")
    expand.Expand()
    time.sleep(OPEN_S)
    option = _find(uia, mod, hwnd, mod.UIA_ListItemControlTypeId, "Large")
    if not option:
        return "no option after Expand"
    kinds = [name for name, pid in PATTERNS.items() if option.GetCurrentPattern(pid)]
    _select(uia, mod, option, hwnd)
    time.sleep(OPEN_S)
    expand.Collapse()
    return f"Expand+Select option {kinds}+Collapse"


def _toggle_state(mod: Any, element: Any) -> object:  # Any: comtypes
    return int(_pattern(element, mod, TOGGLE, "IUIAutomationTogglePattern").CurrentToggleState)


def _is_selected(mod: Any, element: Any) -> object:  # Any: comtypes
    pattern = _pattern(element, mod, SELECTION_ITEM, "IUIAutomationSelectionItemPattern")
    return bool(pattern.CurrentIsSelected)


def _value(mod: Any, element: Any) -> object:  # Any: comtypes
    if not element.GetCurrentPattern(VALUE):
        return "<no Value>"
    return str(_pattern(element, mod, VALUE, "IUIAutomationValuePattern").CurrentValue)


WIDGETS = (
    Widget("checkbox", "UIA_CheckBoxControlTypeId", "Email alerts", _toggle, _toggle_state),
    Widget("tab", "UIA_TabItemControlTypeId", "Details", _select, _is_selected),
    Widget("radio", "UIA_RadioButtonControlTypeId", "Pro", _select, _is_selected),
    Widget("select", "UIA_ComboBoxControlTypeId", "Size", _choose_large, _value),
)


def _measure(widget: Widget, number: int, hwnd: int, state: Callable[[], Any]) -> dict[str, Any]:
    from _ctypes import COMError  # noqa: PLC0415 - CPython on Windows

    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only

    client = UiaClient()
    uia, mod = client._uia, client._uia_module
    element = _find(uia, mod, hwnd, getattr(mod, widget.control_type), widget.label)
    patterns = [name for name, pid in PATTERNS.items() if element.GetCurrentPattern(pid)]
    before = state()
    started = time.perf_counter()
    try:
        detail = widget.act(uia, mod, element, hwnd)
    except (COMError, OSError) as exc:
        detail = f"raised {type(exc).__name__}"
    act_ms = round((time.perf_counter() - started) * 1000, 2)
    time.sleep(OPEN_S)
    after = state()
    return {
        "run": number,
        "widget": widget.name,
        "patterns": patterns,
        "detail": detail,
        "act_ms": act_ms,
        "dom_before": {k: v for k, v in before.items() if k != "log"},
        "dom_after": {k: v for k, v in after.items() if k != "log"},
        "events": after["log"][len(before["log"]) :],
        "uia_after": widget.read(mod, element),
    }


def run(number: int) -> list[dict[str, Any]]:
    from steerai.probes.task_suite import _page  # noqa: PLC0415 - the one-tab reader
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415
    from steerai.run import warm_up  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    wait_until_idle()
    tmp = Path(tempfile.mkdtemp(prefix="steer-widgets-"))
    with PageServer({"/page.html": PAGE}) as server:
        port = _free_port()
        url = f"http://127.0.0.1:{server.port}/page.html"
        proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url, window_size=WINDOW)
        try:
            window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
            page_ws_url(port)
            _in_front(window.hwnd)
            model = WorldModel(UiaClient())
            warm_up(lambda: model.observe(window.hwnd), cap_s=15.0)

            def state() -> Any:  # Any: the page's JSON state
                cdp = _page(port)
                try:
                    return json.loads(_evaluate(cdp, STATE, "state"))
                finally:
                    cdp.close()

            return [_measure(widget, number, window.hwnd, state) for widget in WIDGETS]
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Widget patterns in Chrome through UIA.")
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    for number in range(1, args.runs + 1):
        for row in run(number):
            sys.stdout.write(json.dumps(row) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
