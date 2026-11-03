"""The executor (design 5.9): semantic verbs through live handles, with readback and dispositions.

Pure tests drive the executor with fake COM handles that expose the pattern surface the executor
uses; the Windows test runs the same verbs against a throwaway Notepad.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

import steerai.action.readback as readback_mod
from steerai.action.dispositions import Delivery
from steerai.action.executor import Action, Executor
from steerai.errors import ErrorCode, SteerError
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.windows import wait_window_for_pid
from steerai.world.elements import Element
from steerai.world.model import Snapshot, WorldModel

BOX = (10, 10, 110, 50)
UIA_INVOKE, UIA_VALUE, UIA_TOGGLE, UIA_EXPAND, UIA_SELECTION_ITEM, UIA_SCROLL_ITEM = (
    10000,
    10002,
    10015,
    10005,
    10010,
    10017,
)


@dataclass
class _FakeHandle:
    """Behaves like a cached IUIAutomationElement for the patterns the executor uses."""

    value: str = ""
    toggled: int = 0  # 0 off, 1 on
    expanded: int = 0  # 0 collapsed, 1 expanded
    selected: bool = False
    patterns: set[int] = field(default_factory=lambda: {UIA_INVOKE, UIA_VALUE, UIA_TOGGLE})
    invoked: int = 0
    read_only: bool = False
    calls: list[str] = field(default_factory=list)

    def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802 - COM naming
        if pattern_id not in self.patterns:
            return None
        handle = self

        class _Pattern:
            def QueryInterface(self, _iface: Any) -> Any:  # noqa: N802
                return self

            def Invoke(self) -> None:  # noqa: N802
                handle.invoked += 1
                handle.calls.append("invoke")

            def SetValue(self, text: str) -> None:  # noqa: N802
                handle.value = text
                handle.calls.append(f"set:{text}")

            @property
            def CurrentValue(self) -> str:  # noqa: N802
                return handle.value

            @property
            def CurrentIsReadOnly(self) -> bool:  # noqa: N802
                return handle.read_only

            def Toggle(self) -> None:  # noqa: N802
                handle.toggled = 0 if handle.toggled else 1
                handle.calls.append("toggle")

            @property
            def CurrentToggleState(self) -> int:  # noqa: N802
                return handle.toggled

            def Expand(self) -> None:  # noqa: N802
                handle.expanded = 1

            def Collapse(self) -> None:  # noqa: N802
                handle.expanded = 0

            @property
            def CurrentExpandCollapseState(self) -> int:  # noqa: N802
                return handle.expanded

            def Select(self) -> None:  # noqa: N802
                handle.selected = True

            @property
            def CurrentIsSelected(self) -> bool:  # noqa: N802
                return handle.selected

            def ScrollIntoView(self) -> None:  # noqa: N802
                handle.calls.append("scroll")

        return _Pattern()


def _snapshot(handle: _FakeHandle, *, role: str = "Button", name: str = "Send") -> Snapshot:
    element = Element(id=1, depth=0, role=role, name=name, box=BOX, runtime_id=(7, 1))
    return Snapshot(
        id="s1",
        hwnd=0x10,
        process="test.exe",
        title="t",
        taken_at=0.0,
        elements=[element],
        node_count=1,
        stable=True,
        handles=[handle],
    )


@pytest.fixture
def fast_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    now = {"t": 0.0}

    def sleep(seconds: float) -> None:
        now["t"] += seconds

    monkeypatch.setattr(
        readback_mod, "time", SimpleNamespace(monotonic=lambda: now["t"], sleep=sleep)
    )


def test_click_invokes_and_is_unverified_without_a_postcondition(fast_clock: None) -> None:
    handle = _FakeHandle()
    result = Executor().execute(Action(verb="click", element_id=1), _snapshot(handle))
    assert handle.invoked == 1
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.code is ErrorCode.VERIFY_TIMEOUT
    assert result.ok is False
    assert result.steps[0].label == "InvokePattern.Invoke"
    assert result.steps[0].mechanism == "semantic"


def test_click_with_a_postcondition_is_verified(fast_clock: None) -> None:
    handle = _FakeHandle()
    action = Action(verb="click", element_id=1, postcondition=lambda: handle.invoked == 1)
    result = Executor().execute(action, _snapshot(handle))
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert result.ok is True


def test_set_value_is_verified_by_reading_the_value_back(fast_clock: None) -> None:
    handle = _FakeHandle()
    action = Action(verb="set_value", element_id=1, text="hello")
    result = Executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert handle.value == "hello"
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert result.ok is True


def test_set_value_already_equal_sends_nothing(fast_clock: None) -> None:
    handle = _FakeHandle(value="hello")
    action = Action(verb="set_value", element_id=1, text="hello")
    result = Executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert handle.calls == []
    assert result.ok is True
    assert result.delivery is Delivery.NOT_DELIVERED


def test_set_value_on_a_read_only_field_is_not_actionable(fast_clock: None) -> None:
    handle = _FakeHandle(read_only=True)
    action = Action(verb="set_value", element_id=1, text="hello")
    result = Executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.delivery is Delivery.NOT_DELIVERED
    assert handle.calls == []


def test_set_value_rejects_invalid_text_before_touching_the_element(fast_clock: None) -> None:
    handle = _FakeHandle()
    action = Action(verb="set_value", element_id=1, text="bad\x1b")
    result = Executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert result.code is ErrorCode.INVALID_TEXT
    assert result.retry_safe is True
    assert handle.calls == []


def test_check_is_idempotent_and_uncheck_toggles_once(fast_clock: None) -> None:
    handle = _FakeHandle(toggled=1)
    checked = Executor().execute(
        Action(verb="check", element_id=1), _snapshot(handle, role="CheckBox")
    )
    assert checked.ok is True
    assert handle.calls == []
    unchecked = Executor().execute(
        Action(verb="uncheck", element_id=1), _snapshot(handle, role="CheckBox")
    )
    assert handle.calls == ["toggle"]
    assert unchecked.delivery is Delivery.DELIVERED_VERIFIED


def test_expand_and_select_are_verified_by_their_state(fast_clock: None) -> None:
    handle = _FakeHandle(patterns={UIA_EXPAND, UIA_SELECTION_ITEM})
    expanded = Executor().execute(
        Action(verb="expand", element_id=1), _snapshot(handle, role="TreeItem")
    )
    selected = Executor().execute(
        Action(verb="select", element_id=1), _snapshot(handle, role="ListItem")
    )
    assert expanded.delivery is Delivery.DELIVERED_VERIFIED
    assert selected.delivery is Delivery.DELIVERED_VERIFIED


def test_missing_pattern_is_not_actionable(fast_clock: None) -> None:
    handle = _FakeHandle(patterns=set())
    result = Executor().execute(Action(verb="click", element_id=1), _snapshot(handle))
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.delivery is Delivery.NOT_DELIVERED


def test_unknown_element_id_is_element_not_found(fast_clock: None) -> None:
    result = Executor().execute(Action(verb="click", element_id=9), _snapshot(_FakeHandle()))
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.retry_safe is True


def test_unknown_verb_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown verb"):
        Action(verb="teleport", element_id=1)


def test_result_evidence_never_contains_the_typed_text(fast_clock: None) -> None:
    handle = _FakeHandle()
    typed = "typed-value-42"
    action = Action(verb="set_value", element_id=1, text=typed)
    result = Executor().execute(action, _snapshot(handle, role="Edit", name="Message"))
    assert typed not in str(result.to_dict())


def test_steer_error_from_a_pattern_call_becomes_a_result(fast_clock: None) -> None:
    class _Broken(_FakeHandle):
        def GetCurrentPattern(self, _pattern_id: int) -> Any:  # noqa: N802
            raise SteerError(ErrorCode.APP_CRASHED, "gone")

    result = Executor().execute(Action(verb="click", element_id=1), _snapshot(_Broken()))
    assert result.code is ErrorCode.APP_CRASHED
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN


@pytest.mark.windows
def test_set_value_on_notepad_document_round_trips() -> None:
    proc = subprocess.Popen(["notepad.exe"])  # noqa: S607 - fixed, well-known executable
    try:
        window = wait_window_for_pid(proc.pid)
        time.sleep(0.5)
        model = WorldModel(UiaClient())
        snapshot = model.observe(window.hwnd, cap_s=5.0)
        target = next(e for e in snapshot.elements if e.role in ("Document", "Edit"))
        result = Executor().execute(
            Action(verb="set_value", element_id=target.id, text="steer wrote this"), snapshot
        )
        assert result.ok is True, result.to_dict()
        assert result.delivery is Delivery.DELIVERED_VERIFIED
        again = Executor().execute(
            Action(verb="set_value", element_id=target.id, text="steer wrote this"), snapshot
        )
        assert again.delivery is Delivery.NOT_DELIVERED
        assert again.ok is True
    finally:
        proc.terminate()
