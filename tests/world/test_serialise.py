"""Snapshot serialisation for the write-ahead record and resume (design 5.11): elements only,
never live handles; a round trip evaluates the same predicates."""

from __future__ import annotations

import json

import pytest

from steerai.errors import SteerError
from steerai.world.elements import Element
from steerai.world.model import Snapshot, diff, snapshot_from_json, snapshot_to_json


def _snapshot() -> Snapshot:
    elements = [
        Element(id=0, depth=0, role="Pane", name="Chat", box=(0, 0, 500, 400), runtime_id=(42, 1)),
        Element(
            id=1,
            depth=1,
            role="Edit",
            name="Message",
            box=(10, 350, 300, 390),
            runtime_id=(42, 41),
            parent_id=0,
            automation_id="composer",
            secure=False,
            enabled=True,
            offscreen=False,
        ),
        Element(
            id=2,
            depth=1,
            role="Text",
            name="café · 2h",
            box=(10, 10, 100, 30),
            runtime_id=(),
            parent_id=0,
            offscreen=True,
        ),
    ]
    return Snapshot(
        id="s-before",
        hwnd=0x10,
        process="chrome.exe",
        title="Amir Khan - Chat",
        taken_at=1_700_000_000.5,
        elements=elements,
        node_count=3,
        stable=True,
        minimized=False,
        pid=777,
        handles=[object(), object(), object()],
        cap=150,
    )


def test_round_trip_keeps_every_field_but_the_handles() -> None:
    text = snapshot_to_json(_snapshot())
    restored = snapshot_from_json(text)
    original = _snapshot()
    assert restored.handles == []
    assert restored.id == original.id
    assert restored.hwnd == original.hwnd
    assert restored.process == original.process
    assert restored.title == original.title
    assert restored.taken_at == original.taken_at
    assert restored.node_count == original.node_count
    assert restored.stable is True
    assert restored.pid == 777
    assert restored.cap == 150
    assert restored.elements == original.elements
    assert all(isinstance(e.runtime_id, tuple) for e in restored.elements)
    assert all(isinstance(e.box, tuple) for e in restored.elements)


def test_serialised_form_is_plain_json_with_a_version() -> None:
    payload = json.loads(snapshot_to_json(_snapshot()))
    assert payload["version"] == 1
    assert "handles" not in payload
    assert payload["elements"][0]["role"] == "Pane"


def test_a_restored_snapshot_diffs_against_a_live_one() -> None:
    before = snapshot_from_json(snapshot_to_json(_snapshot()))
    after = _snapshot()
    assert diff(before, after).appeared == []
    with pytest.raises(SteerError, match="no live handles"):
        before.handle(1)


@pytest.mark.parametrize(
    "broken",
    ['{"version": 2}', '{"version": 1}', "[]", "not json", '{"version": 1, "elements": "x"}'],
)
def test_malformed_input_is_refused(broken: str) -> None:
    with pytest.raises(ValueError, match="snapshot"):
        snapshot_from_json(broken)
