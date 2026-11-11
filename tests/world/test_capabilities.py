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
        (Caps(invoke=True), ("invoke",)),
        (Caps(toggle=0), ("toggle:off",)),
        (Caps(toggle=1, invoke=True), ("toggle:on", "invoke")),
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
            Caps(
                toggle=0, selected=False, expanded=0, read_only=False, invoke=True, range_value=True
            ),
            ("toggle:off", "selectable", "collapsed", "editable", "invoke", "range"),
        ),
        (Caps(), ()),
    ],
)
def test_capabilities_become_closed_tokens_in_a_fixed_order(
    caps: Caps, expected: tuple[str, ...]
) -> None:
    assert tokens(caps) == expected


def test_an_unknown_toggle_state_is_no_token() -> None:
    """UIA's ToggleState is 0, 1 or 2; anything else is not a state Steer acts on."""
    assert tokens(Caps(toggle=7)) == ()


def test_the_listing_line_carries_the_tokens_after_the_box_and_flags() -> None:
    switch = _element(caps=("toggle:off", "invoke"))
    assert format_lines([switch]) == ['3   Button "Dark mode" [100,300,180,330] toggle:off invoke']
    covered = _element(caps=("collapsed",), enabled=False, offscreen=True)
    assert format_lines([covered]) == [
        '3   Button "Dark mode" [100,300,180,330] disabled offscreen collapsed'
    ]
    assert format_lines([_element()]) == ['3   Button "Dark mode" [100,300,180,330]']


def test_the_tokens_survive_the_write_ahead_record() -> None:
    element = _element(caps=("toggle:on", "invoke"))
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
    assert back.elements[0].caps == ("toggle:on", "invoke")


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
