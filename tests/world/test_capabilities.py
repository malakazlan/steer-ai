"""What each element can do, as the listing shows it (packet interaction-model-2026-11-12 §1): the
pattern-availability and pattern-state properties from the one cached fetch become closed tokens
in a fixed order, so the planner chooses a verb by capability, never by role name alone (H5 r2:
an ARIA switch is a Button with Toggle, W3C Core-AAM 1.2; Microsoft's control-pattern table)."""

from __future__ import annotations

import json

import pytest

from steerai.world.capabilities import Caps, tokens
from steerai.world.elements import Element, format_lines
from steerai.world.model import Snapshot, snapshot_from_json, snapshot_to_json


def _element(**kw: object) -> Element:
    fields: dict[str, object] = {
        "id": 3,
        "depth": 1,
        "role": "Button",
        "name": "Dark mode",
        "box": (100, 300, 180, 330),
    }
    fields.update(kw)
    return Element(**fields)  # type: ignore[arg-type]  # test builder over the dataclass fields


@pytest.mark.parametrize(
    ("caps", "expected"),
    [
        (Caps(toggle=0), ("toggle:off",)),
        (Caps(toggle=1), ("toggle:on",)),
        (Caps(toggle=2), ("toggle:mixed",)),
        (Caps(selected=False), ("selectable",)),
        (Caps(selected=True), ("selected",)),
        (Caps(expanded=0), ("collapsed",)),
        (Caps(expanded=1), ("expanded",)),
        (Caps(expanded=2), ("expanded",)),  # PartiallyExpanded shows as expanded (W5 reads 1 only)
        (Caps(expanded=3), ()),  # LeafNode: nothing to open or close
        (Caps(read_only=False), ("editable",)),
        (Caps(read_only=True), ("readonly",)),
        (Caps(range_value=True), ("range",)),
        (
            Caps(toggle=0, selected=False, expanded=0, read_only=False, range_value=True),
            ("toggle:off", "selectable", "collapsed", "editable", "range"),
        ),
        (Caps(), ()),
    ],
)
def test_capabilities_become_closed_tokens_in_a_fixed_order(
    caps: Caps, expected: tuple[str, ...]
) -> None:
    assert tokens(caps, "Edit") == expected


@pytest.mark.parametrize(
    "role", ["CheckBox", "RadioButton", "Button", "List", "DataGrid", "ComboBox", "Hyperlink"]
)
def test_a_value_token_is_shown_on_text_fields_only(role: str) -> None:
    """Gallery, Chrome 156: Value with IsReadOnly false on boxes, radios, switches, lists, grids
    and native selects; `editable` there would be noise."""
    assert tokens(Caps(toggle=0, read_only=False), role) == ("toggle:off",)
    assert tokens(Caps(read_only=False), "Document") == ("editable",)


def test_an_unknown_toggle_state_is_no_token() -> None:
    """UIA's ToggleState is 0, 1 or 2; anything else is not a state Steer acts on."""
    assert tokens(Caps(toggle=7), "Button") == ()


def test_the_listing_line_carries_the_tokens_after_the_box_and_flags() -> None:
    switch = _element(caps=("toggle:off",))
    assert format_lines([switch]) == ['3   Button "Dark mode" [100,300,180,330] toggle:off']
    covered = _element(caps=("collapsed",), enabled=False, offscreen=True)
    assert format_lines([covered]) == [
        '3   Button "Dark mode" [100,300,180,330] disabled offscreen collapsed'
    ]
    assert format_lines([_element()]) == ['3   Button "Dark mode" [100,300,180,330]']


def test_the_tokens_survive_the_write_ahead_record() -> None:
    element = _element(caps=("toggle:on",))
    snapshot = Snapshot(
        id="s",
        hwnd=0x10,
        process="chrome.exe",
        title="t",
        taken_at=1.0,
        elements=[element],
        node_count=1,
        stable=True,
    )
    back = snapshot_from_json(snapshot_to_json(snapshot))
    assert back.elements[0].caps == ("toggle:on",)


def test_a_record_written_before_the_tokens_reads_with_none() -> None:
    """Format 1 records (written before this change) stay readable: no tokens."""
    element = _element()
    snapshot = Snapshot(
        id="s",
        hwnd=0x10,
        process="chrome.exe",
        title="t",
        taken_at=1.0,
        elements=[element],
        node_count=1,
        stable=True,
    )
    payload = json.loads(snapshot_to_json(snapshot))
    payload["version"] = 1
    for raw in payload["elements"]:
        raw.pop("caps", None)
    back = snapshot_from_json(json.dumps(payload))
    assert back.elements[0].caps == ()


class _Cached:
    """A cached element whose state properties return UIA's default values when the pattern is
    absent (Microsoft Learn, GetCachedPropertyValueEx): off, not selected, collapsed."""

    def __init__(self, available: set[str]) -> None:
        self.available = available

    def GetCachedPropertyValue(self, property_id: str) -> object:  # noqa: N802 - the COM name
        defaults = {
            "UIA_ToggleToggleStatePropertyId": 0,
            "UIA_SelectionItemIsSelectedPropertyId": False,
            "UIA_ExpandCollapseExpandCollapseStatePropertyId": 0,
            "UIA_ValueIsReadOnlyPropertyId": False,
        }
        if property_id.startswith("UIA_Is") and property_id.endswith("PatternAvailablePropertyId"):
            return property_id in self.available
        return defaults.get(property_id, 0)


class _Mod:
    def __getattr__(self, name: str) -> str:
        return name


def test_a_state_without_its_pattern_is_never_a_capability() -> None:
    """The default-value trap: a plain Button reads ToggleState Off and ExpandCollapseState
    Collapsed from the cache; only the availability flag makes them tokens."""
    from steerai.probes.uia_snapshot import UiaClient  # noqa: PLC0415 - the client's own rule

    client = UiaClient.__new__(UiaClient)
    client._uia_module = _Mod()  # a name-echo module for the property ids
    plain = client._caps(_Cached(set()))
    assert tokens(plain, "Button") == ()
    switch = client._caps(_Cached({"UIA_IsTogglePatternAvailablePropertyId"}))
    assert tokens(switch, "Button") == ("toggle:off",)
