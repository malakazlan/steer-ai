"""World model: observe until stable, resolve ids only against their snapshot, diff by RuntimeId."""

import subprocess
import time

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.windows import wait_window_for_pid
from steerai.world.elements import Element
from steerai.world.model import Snapshot, WorldModel, diff, resolve_in

BOX = (0, 0, 10, 10)


def _el(idx: int, role: str, name: str, rid: tuple[int, ...]) -> Element:
    return Element(id=idx, depth=1, role=role, name=name, box=BOX, runtime_id=rid)


def _snap(*elements: Element, snapshot_id: str = "s1") -> Snapshot:
    return Snapshot(
        id=snapshot_id,
        hwnd=0x10,
        process="test.exe",
        title="t",
        taken_at=0.0,
        elements=list(elements),
        node_count=len(elements),
        stable=True,
        handles=[None] * len(elements),
    )


def test_diff_reports_appeared_and_disappeared_by_runtime_id() -> None:
    before = _snap(_el(1, "Text", "ok", (1,)), _el(2, "Text", "hello", (2,)))
    after = _snap(_el(1, "Text", "ok", (1,)), _el(2, "Text", "hello", (3,)), snapshot_id="s2")
    d = diff(before, after)
    assert [e.runtime_id for e in d.appeared] == [(3,)]
    assert [e.runtime_id for e in d.disappeared] == [(2,)]


def test_resolve_in_matches_runtime_id_and_checks_role_and_name() -> None:
    chosen = _el(2, "Button", "Send", (5, 1))
    snapshot = _snap(_el(1, "Edit", "Message", (4, 1)), chosen)
    fresh = _snap(_el(1, "Button", "Send", (5, 1)), snapshot_id="s2")
    assert resolve_in(snapshot, 2, fresh).runtime_id == (5, 1)


def test_resolve_in_rejects_a_renamed_element() -> None:
    snapshot = _snap(_el(1, "Button", "Send", (5, 1)))
    fresh = _snap(_el(1, "Button", "Delete chat", (5, 1)), snapshot_id="s2")
    with pytest.raises(SteerError) as info:
        resolve_in(snapshot, 1, fresh)
    assert info.value.code is ErrorCode.ELEMENT_NOT_FOUND


def test_resolve_in_rejects_an_unknown_id() -> None:
    snapshot = _snap(_el(1, "Button", "Send", (5, 1)))
    with pytest.raises(SteerError, match="element_not_found"):
        resolve_in(snapshot, 9, snapshot)


@pytest.mark.windows
def test_observe_notepad_until_stable_and_resolve_live() -> None:
    proc = subprocess.Popen(["notepad.exe"])  # noqa: S607 - fixed, well-known executable
    try:
        window = wait_window_for_pid(proc.pid)
        time.sleep(0.5)
        model = WorldModel(UiaClient())
        snapshot = model.observe(window.hwnd, cap_s=5.0)
        assert snapshot.stable
        assert snapshot.node_count >= 1
        assert snapshot.process.lower() == "notepad.exe"
        assert all(e.runtime_id for e in snapshot.elements)
        assert 0 < len(snapshot.lines) <= len(snapshot.elements)
        line_ids = {int(line.split(" ", 1)[0]) for line in snapshot.lines if line[0].isdigit()}
        assert line_ids <= {e.id for e in snapshot.elements}
        target = next(e for e in snapshot.elements if e.role in ("Edit", "Document"))
        fresh = model.observe(window.hwnd, cap_s=5.0)
        resolved = model.resolve(snapshot, target.id, fresh)
        assert resolved.runtime_id == target.runtime_id
        assert model.diff(snapshot, fresh).appeared == []
    finally:
        proc.terminate()
