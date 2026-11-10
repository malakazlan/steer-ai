"""Probe for the widget-verb design (H2 classes A-D, 2026-11-08): which UIA control patterns Chrome
exposes on native and ARIA widgets, whether the semantic action changes the page's own state and
reaches its event handlers, and whether the UIA state reads back the change. A local fixture
page (never the spent H2 page), throwaway profile, semantic UIA calls only (no OS input; the
window is brought in front behind the idle gate). n runs per widget.

Widgets: a native checkbox (Toggle), an ARIA tablist (`role=tab` with aria-selected,
SelectionItem.Select), native radio buttons (SelectionItem.Select), a native select
(ExpandCollapse, then SelectionItem.Select on the option, then Collapse).

Section `tree` (principal review, D-WV REVISE items 5 and 8): where the select's options sit
(in the ComboBox's own subtree, behind ControllerFor, or only from the window root), whether they
exist and Select works while collapsed, the Expand-to-options wait (10 ms polls), the Collapse
readback; and a mixed checkbox (`indeterminate`): its ToggleState and what one Toggle does.

Section `secure` (W2 verdict item 3, WV-09): a password field and a "Show password" checkbox that
turns it into a text field. Whether the field keeps its RuntimeId across the switch, and whether
the snapshot still flags it `secure`: the sticky-secure rule keys on the RuntimeId.

Section `groups` (W3 verdict, blocking item 1): consent questions answered "Yes"/"No" carry
their words in the group label. For radios in a `<fieldset><legend>`, in a `role=radiogroup`
with `aria-label`, and in a plain `<div>` after a `<p>` question, the radio's ancestors as the
world model sees them (role and name, nearest first): does any ancestor carry the question?

Section `choose` (W4, hunter WV-07/36): the executor's own sequence on a native select with no
fixed waits: Expand, poll the ComboBox subtree for the option every 10 ms, SelectionItem.Select,
Collapse at once, then poll ValuePattern for the option. Timings, the Collapse readback, the
option's IsSelected, and the page's trusted events.

Section `shapes` (W4-2/W4-4): how Chrome exposes choosers by label: a `<select multiple>`, a
`<select size=5>`, a plain `<select>`, an `<input list>` with a datalist, and an ARIA combobox
input. Each element's control type, patterns, Value IsReadOnly, and CanSelectMultiple; for a
combo box (W4 verdict item 1: which signal tells an editable one) its children's control types
(Microsoft: an Edit child marks an editable combo box), AriaRole, ClassName, the
LegacyIAccessible state bits, and the ListItems in its own subtree after Expand (then Collapse).

Section `names` (W4 hunter rows W4-09/15/05/19): how Chrome names a select and its options:
an unlabeled select (is it named after its selected option, so a choose renames it?), an option
with a `label` attribute (ListItem name vs Value), an option text with a no-break space, and
an `<optgroup>` (is its label a ListItem?). Each select is chosen once with the zero-wait
sequence and its name, Value and option names are read before and after.

Usage (Windows):
    uv run python -m steerai.probes.widget_patterns --runs 5
    uv run python -m steerai.probes.widget_patterns --section tree --runs 5
    uv run python -m steerai.probes.widget_patterns --section secure --runs 5
    uv run python -m steerai.probes.widget_patterns --section groups --runs 3
    uv run python -m steerai.probes.widget_patterns --section choose --runs 5
    uv run python -m steerai.probes.widget_patterns --section shapes --runs 3
    uv run python -m steerai.probes.widget_patterns --section names --runs 3
    uv run python -m steerai.probes.widget_patterns --section disclosure --runs 3
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
from functools import partial
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
<label><input type="checkbox" id="mixed"> Select all</label>
<label>Colour <select id="sel2"><option>Red</option><option>Blue</option></select></label>
<fieldset><legend>Plan</legend>
  <label><input type="radio" name="plan" id="r1" checked> Basic</label>
  <label><input type="radio" name="plan" id="r2"> Pro</label>
</fieldset>
<script>
window.LOG = [];
function log(id, type, e) { window.LOG.push(id + ":" + type + (e.isTrusted ? "" : ":untrusted")); }
document.getElementById("mixed").indeterminate = true;
for (const id of ["cb", "sel", "sel2", "mixed", "r1", "r2"]) {
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


TREE_STATE = (
    "JSON.stringify({sel2: document.getElementById('sel2').value,"
    " mixed: document.getElementById('mixed').checked,"
    " indeterminate: document.getElementById('mixed').indeterminate, log: window.LOG})"
)
POLL_S, OPTIONS_LIMIT_S = 0.01, 3.0


def _subtree_option(uia: Any, mod: Any, combo: Any, name: str) -> Any:  # Any: comtypes
    cond = uia.CreateAndCondition(
        uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId, mod.UIA_ListItemControlTypeId),
        uia.CreatePropertyCondition(mod.UIA_NamePropertyId, name),
    )
    return combo.FindFirst(mod.TreeScope_Descendants, cond)


def _controller_for(combo: Any, mod: Any) -> int:  # Any: comtypes
    targets = combo.GetCurrentPropertyValue(mod.UIA_ControllerForPropertyId)
    return 0 if targets is None else int(getattr(targets, "Length", 0) or 0)


def _options_row(
    uia: Any, mod: Any, hwnd: int, state: Callable[[], Any]
) -> dict[str, Any]:  # Any: comtypes
    """Where the select's options sit, collapsed and expanded, and the Collapse readback."""
    row: dict[str, Any] = {}
    combo = _find(uia, mod, hwnd, mod.UIA_ComboBoxControlTypeId, "Colour")
    expand = _pattern(combo, mod, EXPAND_COLLAPSE, "IUIAutomationExpandCollapsePattern")
    row["controller_for"] = _controller_for(combo, mod)
    row["collapsed_state"] = int(expand.CurrentExpandCollapseState)
    collapsed = _subtree_option(uia, mod, combo, "Blue")
    row["option_in_subtree_collapsed"] = bool(collapsed)
    if collapsed:
        before = state()
        _select(uia, mod, collapsed, hwnd)
        time.sleep(OPEN_S)
        after = state()
        row["select_while_collapsed"] = {
            "value": after["sel2"],
            "events": after["log"][len(before["log"]) :],
            "expand_state": int(expand.CurrentExpandCollapseState),
        }
    started = time.perf_counter()
    expand.Expand()
    seen_ms = None
    while time.perf_counter() - started < OPTIONS_LIMIT_S:
        if _subtree_option(uia, mod, combo, "Red"):
            seen_ms = round((time.perf_counter() - started) * 1000, 1)
            break
        time.sleep(POLL_S)
    row["expand_to_option_in_subtree_ms"] = seen_ms
    row["option_from_root_expanded"] = bool(
        _find(uia, mod, hwnd, mod.UIA_ListItemControlTypeId, "Red")
    )
    row["expanded_state"] = int(expand.CurrentExpandCollapseState)
    expand.Collapse()
    time.sleep(OPEN_S)
    row["collapse_read_back"] = int(expand.CurrentExpandCollapseState)
    return row


def _mixed_row(
    uia: Any, mod: Any, hwnd: int, state: Callable[[], Any]
) -> dict[str, Any]:  # Any: comtypes
    """A mixed checkbox's ToggleState, then what one Toggle does to it."""
    mixed = _find(uia, mod, hwnd, mod.UIA_CheckBoxControlTypeId, "Select all")
    toggle = _pattern(mixed, mod, TOGGLE, "IUIAutomationTogglePattern")
    first = int(toggle.CurrentToggleState)
    before = state()
    toggle.Toggle()
    time.sleep(OPEN_S)
    after = state()
    return {
        "mixed_toggle_state": first,
        "mixed_after_one_toggle": {
            "toggle_state": int(toggle.CurrentToggleState),
            "checked": after["mixed"],
            "indeterminate": after["indeterminate"],
            "events": after["log"][len(before["log"]) :],
        },
    }


def tree(number: int) -> dict[str, Any]:
    """The select's options in the tree, collapsed and expanded; a mixed checkbox's Toggle."""
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
            client = UiaClient()
            warm_up(lambda: WorldModel(client).observe(window.hwnd), cap_s=15.0)
            uia, mod = client._uia, client._uia_module
            hwnd = window.hwnd

            def state() -> Any:  # Any: the page's JSON state
                cdp = _page(port)
                try:
                    return json.loads(_evaluate(cdp, TREE_STATE, "state"))
                finally:
                    cdp.close()

            row: dict[str, Any] = {"run": number}
            row.update(_options_row(uia, mod, hwnd, state))
            row.update(_mixed_row(uia, mod, hwnd, state))
            return row
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


SECURE_PAGE = """<!doctype html><title>Sign in fixture</title>
<label>Password <input type="password" id="pw" value="fixture-only"></label>
<label><input type="checkbox" id="show"> Show password</label>
<script>
document.getElementById("show").addEventListener("change", (e) => {
  document.getElementById("pw").type = e.target.checked ? "text" : "password";
});
</script>"""


def _fields(snapshot: Any) -> list[dict[str, Any]]:  # Any: a world-model Snapshot
    """Every Edit named Password: its RuntimeId and secure flag, never its value."""
    return [
        {"runtime_id": list(e.runtime_id), "secure": e.secure}
        for e in snapshot.elements
        if e.role == "Edit" and e.name == "Password"
    ]


def secure(number: int) -> dict[str, Any]:
    """The password field before and after one Toggle of "Show password"."""
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415
    from steerai.run import warm_up  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    wait_until_idle()
    tmp = Path(tempfile.mkdtemp(prefix="steer-secure-"))
    with PageServer({"/page.html": SECURE_PAGE}) as server:
        port = _free_port()
        url = f"http://127.0.0.1:{server.port}/page.html"
        proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url, window_size=WINDOW)
        try:
            window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
            page_ws_url(port)
            _in_front(window.hwnd)
            client = UiaClient()
            world = WorldModel(client)
            warm_up(lambda: world.observe(window.hwnd), cap_s=15.0)
            before = _fields(world.observe(window.hwnd))
            uia, mod = client._uia, client._uia_module
            show = _find(uia, mod, window.hwnd, mod.UIA_CheckBoxControlTypeId, "Show password")
            _toggle(uia, mod, show, window.hwnd)
            time.sleep(OPEN_S)
            after = _fields(world.observe(window.hwnd))
            same = bool(before) and any(a["runtime_id"] == before[0]["runtime_id"] for a in after)
            return {"run": number, "before": before, "after": after, "same_runtime_id": same}
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


GROUPS_PAGE = """<!doctype html><title>Consent fixture</title>
<fieldset><legend>I agree to pay the monthly fee</legend>
  <label><input type="radio" name="pay"> Yes</label>
  <label><input type="radio" name="pay"> No</label>
</fieldset>
<div role="radiogroup" aria-label="I consent to data sharing">
  <label><input type="radio" name="share"> Allow</label>
  <label><input type="radio" name="share"> Deny</label>
</div>
<p>Do you accept the terms?</p>
<div>
  <label><input type="radio" name="terms"> Sure</label>
  <label><input type="radio" name="terms"> Never</label>
</div>"""
GROUP_RADIOS = (("fieldset_legend", "Yes"), ("radiogroup_label", "Allow"), ("plain_div", "Sure"))
MAX_ANCESTORS = 3


def _ancestors(snapshot: Any, name: str) -> list[list[str]]:  # Any: a world-model Snapshot
    """The radio's nearest ancestors as (role, name), up to MAX_ANCESTORS."""
    by_id = {e.id: e for e in snapshot.elements}
    radios = [e for e in snapshot.elements if e.role == "RadioButton" and e.name == name]
    if len(radios) != 1:
        return [["radios", str(len(radios))]]
    chain: list[list[str]] = []
    parent = radios[0].parent_id
    while parent is not None and parent in by_id and len(chain) < MAX_ANCESTORS:
        element = by_id[parent]
        chain.append([element.role, element.name])
        parent = element.parent_id
    return chain


def groups(number: int) -> dict[str, Any]:
    """Each radio's ancestor chain on the consent fixture."""
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415
    from steerai.run import warm_up  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    wait_until_idle()
    tmp = Path(tempfile.mkdtemp(prefix="steer-groups-"))
    with PageServer({"/page.html": GROUPS_PAGE}) as server:
        port = _free_port()
        url = f"http://127.0.0.1:{server.port}/page.html"
        proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url, window_size=WINDOW)
        try:
            window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
            page_ws_url(port)
            _in_front(window.hwnd)
            world = WorldModel(UiaClient())
            snapshot = warm_up(lambda: world.observe(window.hwnd), cap_s=15.0)
            row: dict[str, Any] = {"run": number}
            for case, name in GROUP_RADIOS:
                row[case] = _ancestors(snapshot, name)
            return row
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


def _poll(check: Callable[[], bool], limit_s: float) -> float | None:
    """Milliseconds until `check()` holds, polled every POLL_S; None past `limit_s`."""
    started = time.perf_counter()
    while time.perf_counter() - started < limit_s:
        if check():
            return round((time.perf_counter() - started) * 1000, 1)
        time.sleep(POLL_S)
    return None


def _choose_row(
    uia: Any, mod: Any, hwnd: int, state: Callable[[], Any], wanted: str
) -> dict[str, Any]:  # Any: comtypes
    """One zero-wait choose of `wanted` on the Colour select."""
    combo = _find(uia, mod, hwnd, mod.UIA_ComboBoxControlTypeId, "Colour")
    expand = _pattern(combo, mod, EXPAND_COLLAPSE, "IUIAutomationExpandCollapsePattern")
    value = _pattern(combo, mod, VALUE, "IUIAutomationValuePattern")
    before = state()
    row: dict[str, Any] = {"value_before": str(value.CurrentValue)}
    started = time.perf_counter()
    expand.Expand()
    found: list[Any] = []

    def option_seen() -> bool:
        option = _subtree_option(uia, mod, combo, wanted)
        if option:
            found.append(option)
        return bool(option)

    row["option_ms"] = _poll(option_seen, OPTIONS_LIMIT_S)
    if not found:
        expand.Collapse()
        row["collapse_read_back"] = int(expand.CurrentExpandCollapseState)
        return row
    item = _pattern(found[0], mod, SELECTION_ITEM, "IUIAutomationSelectionItemPattern")
    item.Select()
    row["select_ms"] = round((time.perf_counter() - started) * 1000, 1)
    expand.Collapse()
    row["collapse_now"] = int(expand.CurrentExpandCollapseState)
    row["collapse_ms"] = _poll(lambda: int(expand.CurrentExpandCollapseState) == 0, 1.0)
    row["value_ms"] = _poll(lambda: str(value.CurrentValue) == wanted, 1.0)
    row["value_after"] = str(value.CurrentValue)
    row["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
    after = state()
    row["page_value"] = after["sel2"]
    row["events"] = after["log"][len(before["log"]) :]
    return row


def choose(number: int) -> dict[str, Any]:
    """Two zero-wait chooses (Blue, then Red back) on the fixture's Colour select."""
    from steerai.probes.task_suite import _page  # noqa: PLC0415 - the one-tab reader
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415
    from steerai.run import warm_up  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    wait_until_idle()
    tmp = Path(tempfile.mkdtemp(prefix="steer-choose-"))
    with PageServer({"/page.html": PAGE}) as server:
        port = _free_port()
        url = f"http://127.0.0.1:{server.port}/page.html"
        proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url, window_size=WINDOW)
        try:
            window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
            page_ws_url(port)
            _in_front(window.hwnd)
            client = UiaClient()
            warm_up(lambda: WorldModel(client).observe(window.hwnd), cap_s=15.0)
            uia, mod = client._uia, client._uia_module

            def state() -> Any:  # Any: the page's JSON state
                cdp = _page(port)
                try:
                    return json.loads(_evaluate(cdp, TREE_STATE, "state"))
                finally:
                    cdp.close()

            return {
                "run": number,
                "blue": _choose_row(uia, mod, window.hwnd, state, "Blue"),
                "red": _choose_row(uia, mod, window.hwnd, state, "Red"),
            }
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


SHAPES_PAGE = """<!doctype html><title>Chooser fixture</title>
<label>Toppings <select multiple><option>Ham</option><option>Olives</option></select></label>
<label>Sizes <select size="5"><option>S</option><option>M</option><option>L</option>
</select></label>
<label>Colour <select><option>Red</option><option>Blue</option></select></label>
<label>City <input list="cities"></label>
<datalist id="cities"><option value="Lahore"><option value="Karachi"></datalist>
<label for="aria">Fruit</label>
<input id="aria" role="combobox" aria-expanded="false" aria-controls="fruits">
<ul id="fruits" role="listbox" hidden><li role="option">Apple</li></ul>"""
SHAPES = ("Toppings", "Sizes", "Colour", "City", "Fruit")
SELECTION = 10001
LEGACY = 10018
ARIA_ROLE = 30101
CLASS_NAME = 30012


def _combo_signals(uia: Any, mod: Any, element: Any) -> dict[str, Any]:  # Any: comtypes
    """What may tell a select from an editable combo box."""
    walker = uia.RawViewWalker
    children: list[str] = []
    child = walker.GetFirstChildElement(element)
    while child:
        children.append(str(child.CurrentLocalizedControlType))
        child = walker.GetNextSiblingElement(child)
    legacy = _pattern(element, mod, LEGACY, "IUIAutomationLegacyIAccessiblePattern")
    row: dict[str, Any] = {
        "children": children,
        "aria_role": str(element.GetCurrentPropertyValue(ARIA_ROLE)),
        "class_name": str(element.GetCurrentPropertyValue(CLASS_NAME)),
        "legacy_state": int(legacy.CurrentState),
    }
    expand = _pattern(element, mod, EXPAND_COLLAPSE, "IUIAutomationExpandCollapsePattern")
    row["expand_error"] = _com_error(expand.Expand)
    items_cond = uia.CreatePropertyCondition(
        mod.UIA_ControlTypePropertyId, mod.UIA_ListItemControlTypeId
    )
    seen = 0
    started = time.perf_counter()
    while time.perf_counter() - started < 1.0 and not seen:
        seen = element.FindAll(mod.TreeScope_Descendants, items_cond).Length
        time.sleep(POLL_S)
    row["items_in_subtree_after_expand"] = seen
    row["expanded_state"] = int(expand.CurrentExpandCollapseState)
    row["collapse_error"] = _com_error(expand.Collapse)
    row["after_collapse_state"] = int(expand.CurrentExpandCollapseState)
    return row


def _com_error(call: Callable[[], object]) -> str | None:
    """None, or the HRESULT of a failed COM call, so one shape's refusal is measured too."""
    from comtypes import COMError  # noqa: PLC0415 - Windows only

    try:
        call()
    except COMError as exc:
        return hex(exc.hresult & 0xFFFFFFFF)
    return None


def _shape(uia: Any, mod: Any, hwnd: int, name: str) -> dict[str, Any]:  # Any: comtypes
    cond = uia.CreatePropertyCondition(mod.UIA_NamePropertyId, name)
    found = uia.ElementFromHandle(hwnd).FindAll(mod.TreeScope_Descendants, cond)
    rows: list[dict[str, Any]] = []
    for index in range(found.Length):
        element = found.GetElement(index)
        row: dict[str, Any] = {
            "type": str(element.CurrentLocalizedControlType),
            "patterns": [k for k, pid in PATTERNS.items() if element.GetCurrentPattern(pid)],
        }
        if element.GetCurrentPattern(VALUE):
            value = _pattern(element, mod, VALUE, "IUIAutomationValuePattern")
            row["read_only"] = bool(value.CurrentIsReadOnly)
        if element.GetCurrentPattern(SELECTION):
            selection = _pattern(element, mod, SELECTION, "IUIAutomationSelectionPattern")
            row["multiple"] = bool(selection.CurrentCanSelectMultiple)
        if int(element.CurrentControlType) == mod.UIA_ComboBoxControlTypeId:
            row.update(_combo_signals(uia, mod, element))
            time.sleep(0.3)  # the popup closes before the next shape is read
        rows.append(row)
    return {"name": name, "elements": rows}


def shapes(number: int) -> dict[str, Any]:
    """The chooser shapes on one fixture page."""
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415
    from steerai.run import warm_up  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    wait_until_idle()
    tmp = Path(tempfile.mkdtemp(prefix="steer-shapes-"))
    with PageServer({"/page.html": SHAPES_PAGE}) as server:
        port = _free_port()
        url = f"http://127.0.0.1:{server.port}/page.html"
        proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url, window_size=WINDOW)
        try:
            window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
            page_ws_url(port)
            _in_front(window.hwnd)
            client = UiaClient()
            warm_up(lambda: WorldModel(client).observe(window.hwnd), cap_s=15.0)
            uia, mod = client._uia, client._uia_module
            return {"run": number, "shapes": [_shape(uia, mod, window.hwnd, n) for n in SHAPES]}
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


NBSP = chr(0xA0)
NAMES_PAGE = (
    "<!doctype html><title>Names fixture</title>"
    '<select id="bare"><option>Small</option><option>Large</option></select>'
    '<label>Country <select id="labelled"><option label="USA">United States</option>'
    "<option>Canada</option></select></label>"
    f"<label>Seat <select><option>Aisle</option><option>Window{NBSP}seat</option></select></label>"
    "<label>Pet <select><optgroup label=Mammals><option>Cat</option></optgroup>"
    "<optgroup label=Birds><option>Owl</option></optgroup></select></label>"
)
NAMED_CHOICES = (("bare", "Large"), ("Country", "Canada"), ("Seat", None), ("Pet", "Owl"))
"""(how the select is found: its id for the bare one, else its label; the option to choose,
None to choose the second option whatever its name)."""


def _combo_named(uia: Any, mod: Any, hwnd: int, key: str) -> Any:  # Any: comtypes
    if key == "bare":
        cond = uia.CreatePropertyCondition(mod.UIA_AutomationIdPropertyId, "bare")
        return uia.ElementFromHandle(hwnd).FindFirst(mod.TreeScope_Descendants, cond)
    return _find(uia, mod, hwnd, mod.UIA_ComboBoxControlTypeId, key)


def _items(uia: Any, mod: Any, combo: Any) -> list[dict[str, Any]]:  # Any: comtypes
    cond = uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId, mod.UIA_ListItemControlTypeId)
    found = combo.FindAll(mod.TreeScope_Descendants, cond)
    rows = []
    for index in range(found.Length):
        item = found.GetElement(index)
        rows.append(
            {
                "name": [ord(c) for c in str(item.CurrentName)]
                if NBSP in str(item.CurrentName)
                else str(item.CurrentName),
                "selection_item": bool(item.GetCurrentPattern(SELECTION_ITEM)),
                "enabled": bool(item.CurrentIsEnabled),
            }
        )
    return rows


def _named_row(uia: Any, mod: Any, hwnd: int, key: str, option: str | None) -> dict[str, Any]:
    combo = _combo_named(uia, mod, hwnd, key)  # Any: comtypes throughout
    if not combo:
        return {"found": False}
    expand = _pattern(combo, mod, EXPAND_COLLAPSE, "IUIAutomationExpandCollapsePattern")
    value = _pattern(combo, mod, VALUE, "IUIAutomationValuePattern")
    row: dict[str, Any] = {
        "name_before": str(combo.CurrentName),
        "value_before": str(value.CurrentValue),
    }
    expand.Expand()
    _poll(lambda: bool(_items(uia, mod, combo)), OPTIONS_LIMIT_S)
    items = _items(uia, mod, combo)
    row["items"] = items
    choosable = [i for i in items if i["selection_item"]]
    target_name = (
        option if option is not None else (choosable[1]["name"] if len(choosable) > 1 else None)
    )
    cond = uia.CreateAndCondition(
        uia.CreatePropertyCondition(mod.UIA_ControlTypePropertyId, mod.UIA_ListItemControlTypeId),
        uia.CreatePropertyCondition(
            mod.UIA_NamePropertyId,
            "".join(chr(c) for c in target_name) if isinstance(target_name, list) else target_name,
        ),
    )
    target = combo.FindFirst(mod.TreeScope_Descendants, cond) if target_name is not None else None
    if target:
        _pattern(target, mod, SELECTION_ITEM, "IUIAutomationSelectionItemPattern").Select()
    expand.Collapse()
    _poll(lambda: int(expand.CurrentExpandCollapseState) == 0, 1.0)
    time.sleep(0.2)
    after = str(value.CurrentValue)
    row["value_after"] = [ord(c) for c in after] if NBSP in after else after
    row["name_after"] = str(combo.CurrentName)
    return row


def names(number: int) -> dict[str, Any]:
    """Names and values of the four selects, before and after one choose each."""
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415
    from steerai.run import warm_up  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    wait_until_idle()
    tmp = Path(tempfile.mkdtemp(prefix="steer-names-"))
    with PageServer({"/page.html": NAMES_PAGE}) as server:
        port = _free_port()
        url = f"http://127.0.0.1:{server.port}/page.html"
        proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url, window_size=WINDOW)
        try:
            window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
            page_ws_url(port)
            _in_front(window.hwnd)
            client = UiaClient()
            warm_up(lambda: WorldModel(client).observe(window.hwnd), cap_s=15.0)
            uia, mod = client._uia, client._uia_module
            row: dict[str, Any] = {"run": number}
            for key, option in NAMED_CHOICES:
                row[key] = _named_row(uia, mod, window.hwnd, key, option)
            return row
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


DISCLOSURE_PAGE = """<!doctype html><title>Disclosure fixture</title>
<button id="acc" aria-expanded="false" aria-controls="panel">Shipping questions</button>
<div id="panel" hidden><p>Orders ship within two days.</p></div>
<p id="lb-label">Destinations</p>
<ul id="lb" role="listbox" aria-labelledby="lb-label" style="height:120px;overflow:auto">
<li role="option" aria-selected="false">Accra</li><li role="option" aria-selected="false">Agra</li>
<li role="option" aria-selected="false">Baku</li><li role="option" aria-selected="false">Cairo</li>
<li role="option" aria-selected="false">Delhi</li><li role="option" aria-selected="false">Hanoi</li>
<li role="option" aria-selected="false">Kyoto</li><li role="option" aria-selected="false">Lima</li>
</ul>
<label>Size <select id="sz" size="4"><option>S</option><option>M</option><option>L</option>
<option>XL</option></select></label>
<script>
window.LOG = [];
function log(id, type, e) { window.LOG.push(id + ":" + type + (e.isTrusted ? "" : ":untrusted")); }
const acc = document.getElementById("acc");
acc.addEventListener("click", (e) => {
  const open = acc.getAttribute("aria-expanded") !== "true";
  acc.setAttribute("aria-expanded", open ? "true" : "false");
  document.getElementById("panel").hidden = !open;
  log("acc", "click", e);
});
for (const li of document.querySelectorAll("#lb li")) {
  li.addEventListener("click", (e) => {
    for (const o of document.querySelectorAll("#lb li")) o.setAttribute("aria-selected", o === li);
    log("lb:" + li.textContent, "click", e);
  });
}
for (const t of ["click", "input", "change"]) {
  document.getElementById("sz").addEventListener(t, (e) => log("sz", t, e));
}
</script>"""
DISCLOSURE_STATE = (
    "JSON.stringify({expanded: document.getElementById('acc').getAttribute('aria-expanded'),"
    " panel_hidden: document.getElementById('panel').hidden,"
    " kyoto: document.querySelector('#lb li:nth-child(7)').getAttribute('aria-selected'),"
    " sz: document.getElementById('sz').value, log: window.LOG})"
)


def _patterns_of(element: Any) -> list[str]:  # Any: comtypes
    return [k for k, pid in PATTERNS.items() if element.GetCurrentPattern(pid)]


def _disclosure_row(uia: Any, mod: Any, hwnd: int, state: Callable[[], Any]) -> dict[str, Any]:
    """W5: the accordion button's patterns and ExpandCollapseState after Expand and Collapse."""
    button = _find(uia, mod, hwnd, mod.UIA_ButtonControlTypeId, "Shipping questions")
    row: dict[str, Any] = {"button_patterns": _patterns_of(button)}
    if "ExpandCollapse" not in row["button_patterns"]:
        return row
    expand = _pattern(button, mod, EXPAND_COLLAPSE, "IUIAutomationExpandCollapsePattern")
    row["state_before"] = int(expand.CurrentExpandCollapseState)
    before = state()
    expand.Expand()
    row["expand_ms"] = _poll(lambda: int(expand.CurrentExpandCollapseState) == 1, 1.0)
    row["state_after_expand"] = int(expand.CurrentExpandCollapseState)
    after = state()
    row["page_after_expand"] = {
        "expanded": after["expanded"],
        "panel_hidden": after["panel_hidden"],
    }
    row["events_expand"] = after["log"][len(before["log"]) :]
    text = _find(uia, mod, hwnd, mod.UIA_TextControlTypeId, "Orders ship within two days.")
    row["panel_text_in_tree"] = bool(text)
    expand.Collapse()
    row["collapse_ms"] = _poll(lambda: int(expand.CurrentExpandCollapseState) == 0, 1.0)
    row["state_after_collapse"] = int(expand.CurrentExpandCollapseState)
    final = state()
    row["events_collapse"] = final["log"][len(after["log"]) :]
    return row


def _is_selected_now(item_pattern: Any) -> bool:  # Any: comtypes
    return bool(item_pattern.CurrentIsSelected)


def _listbox_row(uia: Any, mod: Any, hwnd: int, state: Callable[[], Any]) -> dict[str, Any]:
    """W3b: the ARIA listbox and a `<select size>`: List Selection / CanSelectMultiple, the
    items' SelectionItem, and what Select does to the page."""
    row: dict[str, Any] = {}
    for label, item_name, key in (("Destinations", "Kyoto", "kyoto"), ("Size", "L", "sz")):
        lst = _find(uia, mod, hwnd, mod.UIA_ListControlTypeId, label)
        part: dict[str, Any] = {"list_found": bool(lst)}
        if lst:
            part["list_patterns"] = _patterns_of(lst)
            if lst.GetCurrentPattern(SELECTION):
                sel = _pattern(lst, mod, SELECTION, "IUIAutomationSelectionPattern")
                part["multiple"] = bool(sel.CurrentCanSelectMultiple)
            item = _subtree_option(uia, mod, lst, item_name)
            part["item_found"] = bool(item)
            if item:
                part["item_patterns"] = _patterns_of(item)
                part["item_offscreen"] = bool(item.CurrentIsOffscreen)
                if item.GetCurrentPattern(SELECTION_ITEM):
                    before = state()
                    si = _pattern(item, mod, SELECTION_ITEM, "IUIAutomationSelectionItemPattern")
                    si.Select()
                    part["selected_ms"] = _poll(partial(_is_selected_now, si), 1.0)
                    after = state()
                    part["page"] = after[key]
                    part["events"] = after["log"][len(before["log"]) :]
                    part["item_offscreen_after"] = bool(item.CurrentIsOffscreen)
        row[label] = part
    return row


def disclosure(number: int) -> dict[str, Any]:
    """One run of the W5 and W3b rows on the disclosure fixture."""
    from steerai.probes.task_suite import _page  # noqa: PLC0415 - the one-tab reader
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - Windows only
    from steerai.probes.windows import wait_window_for_pid  # noqa: PLC0415
    from steerai.run import warm_up  # noqa: PLC0415
    from steerai.world.model import WorldModel  # noqa: PLC0415

    wait_until_idle()
    tmp = Path(tempfile.mkdtemp(prefix="steer-disclosure-"))
    with PageServer({"/page.html": DISCLOSURE_PAGE}) as server:
        port = _free_port()
        url = f"http://127.0.0.1:{server.port}/page.html"
        proc = launch_chrome(CHROME_DEFAULT, tmp / "profile", port, url, window_size=WINDOW)
        try:
            window = wait_window_for_pid(proc.pid, image="chrome.exe", process=proc)
            page_ws_url(port)
            _in_front(window.hwnd)
            client = UiaClient()
            warm_up(lambda: WorldModel(client).observe(window.hwnd), cap_s=15.0)
            uia, mod = client._uia, client._uia_module

            def state() -> Any:  # Any: the page's JSON state
                cdp = _page(port)
                try:
                    return json.loads(_evaluate(cdp, DISCLOSURE_STATE, "state"))
                finally:
                    cdp.close()

            return {
                "run": number,
                "disclosure": _disclosure_row(uia, mod, window.hwnd, state),
                "listbox": _listbox_row(uia, mod, window.hwnd, state),
            }
        finally:
            stop_chrome(proc, tmp / "profile")
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Widget patterns in Chrome through UIA.")
    parser.add_argument("--runs", type=int, default=5)
    choices = ("actions", "tree", "secure", "groups", "choose", "shapes", "names", "disclosure")
    parser.add_argument("--section", choices=choices, default="actions")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    for number in range(1, args.runs + 1):
        if args.section != "actions":
            sections = {
                "tree": tree,
                "secure": secure,
                "groups": groups,
                "choose": choose,
                "shapes": shapes,
                "names": names,
                "disclosure": disclosure,
            }
            row = sections[args.section](number)
            sys.stdout.write(json.dumps(row) + "\n")
            sys.stdout.flush()
            continue
        for row in run(number):
            sys.stdout.write(json.dumps(row) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
