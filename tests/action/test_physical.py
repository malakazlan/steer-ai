"""The physical executor (design 5.9 [v1.2]): click at a point, paced typing and chords through
SendInput, behind the same admission, readback and disposition contract as the semantic executor.

Pure tests drive it with the shared fake handle and a fake input port; the Windows tests type into
a throwaway Notepad.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

import steerai.action.actionability as actionability_mod
import steerai.action.physical as physical_mod
import steerai.action.readback as readback_mod
from steerai.action.dispositions import Delivery
from steerai.action.execution import UiaBindings
from steerai.action.keyboard import InjectionError
from steerai.action.physical import MAX_UNITS_PER_STEP, PhysicalAction, PhysicalExecutor
from steerai.action.sanitize import NewlineBinding
from steerai.errors import ErrorCode
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.win_input import acquire_foreground
from steerai.probes.windows import wait_window_for_pid
from steerai.world.elements import Element
from steerai.world.model import Snapshot, WorldModel
from tests.action.fakes import (
    APPROVAL,
    BOX,
    TYPE_IDS,
    UIA_VALUE,
    FakeHandle,
    FakeWindows,
)

TARGET_ID = (7, 1)
CHILD_ID = (7, 2)
PARENT_ID = (7, 0)
CENTRE = (60, 30)


@dataclass
class FakeInput:
    """Stands in for the Win32 side (`LiveInput`); records every injected event."""

    sink: FakeHandle | None = None
    """The handle whose value receives typed text, as the app would."""
    transform: Callable[[str], str] | None = None
    """What the app does to the value after each burst (autocomplete, truncation)."""
    foreground: int = 0x10
    foreground_name: str = "target.exe"
    acquire_fails: bool = False
    acquire_procedure: str = "already"
    modifiers: list[str] = field(default_factory=list)
    buttons: list[str] = field(default_factory=list)
    root_at: int = 0x10
    hits: list[tuple[int, ...]] = field(default_factory=lambda: [TARGET_ID])
    focused: tuple[int, ...] = TARGET_ID
    integrity: int | None = 0x2000
    caller: int = 0x2000
    dpi: bool = True
    desktop: str = "Default"
    filters: bool = False
    hung: bool = False
    own: int = 4242
    window_box: tuple[int, int, int, int] = (0, 0, 1000, 800)
    cursor_override: tuple[int, int] | None = None
    double_click_s: float = 0.5
    double_click_px: int = 4
    fail_chunk: int | None = None
    """Raise InjectionError on this 1-based burst."""
    after_burst: Callable[[FakeInput, int], None] | None = None
    """Hook after each burst (1-based index) to flip foreground, focus or modifiers."""
    moves: list[tuple[int, int]] = field(default_factory=list)
    clicks: list[tuple[int, int]] = field(default_factory=list)
    bursts: list[str] = field(default_factory=list)
    chords: list[str] = field(default_factory=list)

    def dpi_aware(self) -> bool:
        return self.dpi

    def input_desktop_name(self) -> str:
        return self.desktop

    def accessibility_filters_active(self) -> bool:
        return self.filters

    def own_pid(self) -> int:
        return self.own

    def is_hung(self, _hwnd: int) -> bool:
        return self.hung

    def integrity_level(self, _pid: int) -> int | None:
        return self.integrity

    def caller_integrity_level(self) -> int:
        return self.caller

    def window_rect(self, _hwnd: int) -> tuple[int, int, int, int]:
        return self.window_box

    def root_hwnd_at(self, _x: int, _y: int) -> int:
        return self.root_at

    def runtime_id_at(self, _x: int, _y: int) -> tuple[int, ...]:
        if len(self.hits) > 1:
            return self.hits.pop(0)
        return self.hits[0]

    def physical_modifiers_down(self) -> list[str]:
        return list(self.modifiers)

    def physical_buttons_down(self) -> list[str]:
        return list(self.buttons)

    def foreground_hwnd(self) -> int:
        return self.foreground

    def foreground_process(self) -> str:
        return self.foreground_name

    def acquire_foreground(self, hwnd: int) -> str:
        if self.acquire_fails:
            raise OSError("could not bring hwnd to the foreground")
        self.foreground = hwnd
        return self.acquire_procedure

    def focused_runtime_id(self) -> tuple[int, ...]:
        return self.focused

    def double_click_time_s(self) -> float:
        return self.double_click_s

    def double_click_size_px(self) -> int:
        return self.double_click_px

    def move_cursor(self, x: int, y: int) -> None:
        self.moves.append((x, y))

    def cursor_pos(self) -> tuple[int, int]:
        if self.cursor_override is not None:
            return self.cursor_override
        return self.moves[-1] if self.moves else (0, 0)

    def click_at(self, x: int, y: int) -> None:
        self.clicks.append((x, y))

    def _burst(self, label: str, text: str) -> None:
        self.bursts.append(label)
        index = len(self.bursts)
        if self.fail_chunk == index:
            raise InjectionError("SendInput sent 3/16", inserted=3, sent=16)
        if self.sink is not None:
            current = self.sink.value or ""
            self.sink.value = current + text
            if self.transform is not None:
                self.sink.value = self.transform(self.sink.value)
        if self.after_burst is not None:
            self.after_burst(self, index)

    def type_text(self, text: str) -> None:
        self._burst(f"text:{len(text)}", text)

    def press(self, chord: str, _approval: Any = None) -> None:
        self.chords.append(chord)
        self._burst(f"chord:{chord}", "\n" if chord == "shift+enter" else "")


@pytest.fixture
def fast_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    now = {"t": 0.0}

    def sleep(seconds: float) -> None:
        now["t"] += seconds

    fake = SimpleNamespace(monotonic=lambda: now["t"], sleep=sleep, perf_counter=lambda: now["t"])
    for module in (readback_mod, actionability_mod, physical_mod):
        monkeypatch.setattr(module, "time", fake)
    return now


def _executor(inputs: FakeInput, *, windows: FakeWindows | None = None) -> PhysicalExecutor:
    return PhysicalExecutor(
        uia=UiaBindings(control_types={v: k for k, v in TYPE_IDS.items()}, interfaces={}),
        windows=windows or FakeWindows(),
        clock=lambda: 0.0,
        inputs=inputs,
    )


def _snapshot(
    handle: FakeHandle,
    *,
    role: str = "Button",
    name: str = "Send",
    secure: bool = False,
    offscreen: bool = False,
    minimized: bool = False,
    pid: int = 777,
) -> Snapshot:
    handle.control_type = TYPE_IDS[role]
    handle.name = name
    parent = Element(
        id=0, depth=0, role="Group", name="", box=(0, 0, 500, 400), runtime_id=PARENT_ID
    )
    element = Element(
        id=1,
        depth=1,
        role=role,
        name=name,
        box=BOX,
        runtime_id=TARGET_ID,
        parent_id=0,
        secure=secure,
        offscreen=offscreen,
    )
    child = Element(
        id=2, depth=2, role="Text", name=name, box=BOX, runtime_id=CHILD_ID, parent_id=1
    )
    parent_handle = FakeHandle(control_type=50026, name="")
    child_handle = FakeHandle(control_type=50020, name=name)
    return Snapshot(
        id="s1",
        hwnd=0x10,
        process="target.exe",
        title="t",
        taken_at=0.0,
        elements=[parent, element, child],
        node_count=3,
        stable=True,
        minimized=minimized,
        pid=pid,
        handles=[parent_handle, handle, child_handle],
    )


def _edit(value: str = "") -> FakeHandle:
    return FakeHandle(value=value, patterns={UIA_VALUE})


def _type(text: str, **kwargs: Any) -> PhysicalAction:
    return PhysicalAction(verb="type", element_id=1, text=text, **kwargs)


CLICK = PhysicalAction(verb="click", element_id=1)


# ---- construction -----------------------------------------------------------------------------


def test_unknown_verb_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown verb"):
        PhysicalAction(verb="drag", element_id=1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"verb": "type", "element_id": 1},
        {"verb": "press", "element_id": 1},
        {"verb": "click", "element_id": 1, "text": "x"},
        {"verb": "click", "element_id": 1, "chord": "ctrl+s"},
        {"verb": "type", "element_id": 1, "text": "x", "chord": "ctrl+s"},
        {"verb": "press", "element_id": 1, "chord": "ctrl+s", "text": "x"},
        {"verb": "click", "element_id": 1, "newline": NewlineBinding("shift+enter")},
    ],
)
def test_parameters_must_match_the_verb(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="goes with"):
        PhysicalAction(**kwargs)


def test_approval_must_be_typed() -> None:
    with pytest.raises(TypeError, match="GateApproval"):
        PhysicalAction(verb="press", element_id=1, chord="alt+f4", approval=True)  # type: ignore[arg-type]


def test_action_repr_hides_the_text() -> None:
    assert "hunter2" not in repr(_type("hunter2"))


# ---- click ------------------------------------------------------------------------------------


def test_click_moves_checks_the_cursor_and_clicks_the_live_centre(fast_clock: Any) -> None:
    inputs = FakeInput()
    result = _executor(inputs).execute(CLICK, _snapshot(FakeHandle()))
    assert inputs.moves == [CENTRE]
    assert inputs.clicks == [CENTRE]
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.code is ErrorCode.VERIFY_TIMEOUT
    assert result.steps[-1].label == "mouse.click"
    assert result.steps[-1].mechanism == "physical"
    assert result.evidence == {
        "reason": "no_postcondition",
        "clicked_at": [60, 30],
        "foreground_was_target": True,
        "hit_after": "target",
    }


def test_click_with_a_postcondition_is_verified(fast_clock: Any) -> None:
    inputs = FakeInput()
    action = PhysicalAction(verb="click", element_id=1, postcondition=lambda: bool(inputs.clicks))
    result = _executor(inputs).execute(action, _snapshot(FakeHandle()))
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert result.ok is True


def test_click_uses_the_live_rectangle_not_the_snapshot_box(fast_clock: Any) -> None:
    handle = FakeHandle(box=(200, 200, 300, 240))
    inputs = FakeInput()
    _executor(inputs).execute(CLICK, _snapshot(handle))
    assert inputs.clicks == [(250, 220)]


def test_click_on_a_background_window_records_it(fast_clock: Any) -> None:
    inputs = FakeInput(foreground=0x99)
    result = _executor(inputs).execute(CLICK, _snapshot(FakeHandle()))
    assert inputs.clicks == [CENTRE]
    assert result.evidence["foreground_was_target"] is False


def test_click_that_lands_on_something_else_afterwards_is_recorded(fast_clock: Any) -> None:
    inputs = FakeInput(hits=[TARGET_ID, TARGET_ID, (9, 9)])
    result = _executor(inputs).execute(CLICK, _snapshot(FakeHandle()))
    assert result.evidence["hit_after"] == "other"


def test_second_click_waits_out_the_double_click_window(fast_clock: Any) -> None:
    inputs = FakeInput(double_click_s=0.5)
    executor = _executor(inputs)
    executor.execute(CLICK, _snapshot(FakeHandle()))
    first = fast_clock["t"]
    executor.execute(CLICK, _snapshot(FakeHandle()))
    assert fast_clock["t"] - first >= 0.5
    assert len(inputs.clicks) == 2


def test_second_click_far_away_does_not_wait(fast_clock: Any) -> None:
    inputs = FakeInput(double_click_s=0.5)
    executor = _executor(inputs)
    executor.execute(CLICK, _snapshot(FakeHandle()))
    first = fast_clock["t"]
    executor.execute(CLICK, _snapshot(FakeHandle(box=(400, 400, 500, 440))))
    assert fast_clock["t"] - first < 0.5


def test_element_that_moved_after_the_cursor_arrived_is_not_clicked(fast_clock: Any) -> None:
    handle = FakeHandle()
    inputs = FakeInput()

    def moved(x: int, y: int) -> None:
        inputs.moves.append((x, y))
        handle.box = (300, 300, 400, 340)

    inputs.move_cursor = moved  # type: ignore[method-assign]
    result = _executor(inputs).execute(CLICK, _snapshot(handle))
    assert inputs.clicks == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == "target_moved"


def test_cursor_that_did_not_land_on_the_point_aborts_before_the_click(fast_clock: Any) -> None:
    inputs = FakeInput(cursor_override=(61, 30))
    result = _executor(inputs).execute(CLICK, _snapshot(FakeHandle()))
    assert inputs.clicks == []
    assert result.evidence["detail"] == "cursor_mismatch"


# ---- admission: platform and target state (PX-01, 06, 11, 16..21, 27) -------------------------


@pytest.mark.parametrize(
    ("inputs", "code", "detail"),
    [
        (FakeInput(dpi=False), ErrorCode.PERCEPTION_DEGRADED, "dpi_context"),
        (FakeInput(desktop="Winlogon"), ErrorCode.SECURE_DESKTOP, "secure_desktop"),
        (FakeInput(filters=True), ErrorCode.ELEMENT_NOT_ACTIONABLE, "accessibility_filter"),
        (FakeInput(own=777), ErrorCode.GATE_BLOCKED, "self_target"),
        (FakeInput(hung=True), ErrorCode.ELEMENT_NOT_ACTIONABLE, "hung"),
        (FakeInput(integrity=None), ErrorCode.ELEMENT_NOT_ACTIONABLE, "integrity_blocked"),
        (FakeInput(integrity=0x3000), ErrorCode.ELEMENT_NOT_ACTIONABLE, "integrity_blocked"),
        (FakeInput(root_at=0x99), ErrorCode.ELEMENT_NOT_ACTIONABLE, "covered"),
        (FakeInput(hits=[(9, 9)]), ErrorCode.ELEMENT_NOT_ACTIONABLE, "covered_in_window"),
        (FakeInput(hits=[PARENT_ID]), ErrorCode.ELEMENT_NOT_ACTIONABLE, "hit_test_miss"),
        (FakeInput(modifiers=["ctrl"]), ErrorCode.FOCUS_LOST, "modifier_held"),
        (FakeInput(buttons=["left"]), ErrorCode.FOCUS_LOST, "button_held"),
        (FakeInput(window_box=(500, 500, 900, 900)), ErrorCode.ELEMENT_NOT_ACTIONABLE, "clipped"),
    ],
)
def test_platform_and_target_state_refusals_send_nothing(
    fast_clock: Any, inputs: FakeInput, code: ErrorCode, detail: str
) -> None:
    result = _executor(inputs).execute(CLICK, _snapshot(FakeHandle()))
    assert inputs.clicks == []
    assert inputs.moves == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is code
    assert result.evidence["detail"] == detail


def test_minimized_window_is_refused(fast_clock: Any) -> None:
    result = _executor(FakeInput()).execute(CLICK, _snapshot(FakeHandle(), minimized=True))
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == "minimized"


def test_live_offscreen_element_is_refused(fast_clock: Any) -> None:
    result = _executor(FakeInput()).execute(CLICK, _snapshot(FakeHandle(offscreen=True)))
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == "offscreen"


def test_hit_on_a_descendant_of_the_target_is_accepted(fast_clock: Any) -> None:
    inputs = FakeInput(hits=[CHILD_ID])
    result = _executor(inputs).execute(CLICK, _snapshot(FakeHandle()))
    assert inputs.clicks == [CENTRE]
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED


def test_partially_clipped_element_is_clicked_in_its_visible_part(fast_clock: Any) -> None:
    inputs = FakeInput(window_box=(0, 0, 60, 800))
    _executor(inputs).execute(CLICK, _snapshot(FakeHandle()))
    assert inputs.clicks == [(35, 30)]


def test_moving_element_is_unstable(fast_clock: Any) -> None:
    class _Moving(FakeHandle):
        boxes = iter([(10, 10, 110, 50), (20, 10, 120, 50)])

        @property
        def CurrentBoundingRectangle(self) -> Any:  # noqa: N802
            left, top, right, bottom = next(self.boxes)
            return SimpleNamespace(left=left, top=top, right=right, bottom=bottom)

    handle = _Moving()
    result = _executor(FakeInput()).execute(CLICK, _snapshot(handle))
    assert result.code is ErrorCode.UNSTABLE_UI
    assert result.delivery is Delivery.NOT_DELIVERED


def test_shared_admission_still_applies(fast_clock: Any) -> None:
    handle = FakeHandle()
    snapshot = _snapshot(handle, name="Send")
    handle.name = "Delete"
    result = _executor(FakeInput()).execute(CLICK, snapshot)
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.evidence["detail"] == "identity_changed"


def test_secure_field_needs_approval_before_anything_else(fast_clock: Any) -> None:
    inputs = FakeInput()
    result = _executor(inputs).execute(
        _type("s3cret"), _snapshot(_edit(), role="Edit", secure=True)
    )
    assert result.code is ErrorCode.GATE_BLOCKED
    assert inputs.bursts == []


# ---- press ------------------------------------------------------------------------------------


def test_press_brings_the_window_forward_and_sends_the_chord_once(fast_clock: Any) -> None:
    inputs = FakeInput(foreground=0x99, acquire_procedure="alt-tap")
    action = PhysicalAction(verb="press", element_id=1, chord="ctrl+s")
    result = _executor(inputs).execute(action, _snapshot(FakeHandle()))
    assert inputs.chords == ["ctrl+s"]
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.steps[-1].label == "keyboard.chord"
    assert result.evidence == {"reason": "no_postcondition", "foreground": "alt-tap"}


def test_press_with_a_postcondition_is_verified(fast_clock: Any) -> None:
    inputs = FakeInput()
    action = PhysicalAction(
        verb="press", element_id=1, chord="ctrl+s", postcondition=lambda: bool(inputs.chords)
    )
    assert _executor(inputs).execute(action, _snapshot(FakeHandle())).ok is True


@pytest.mark.parametrize(
    ("chord", "detail"), [("alt+f4", "blocked_chord"), ("ctrl+v", "routed_chord")]
)
def test_blocked_and_routed_chords_are_refused_before_sending(
    fast_clock: Any, chord: str, detail: str
) -> None:
    inputs = FakeInput()
    action = PhysicalAction(verb="press", element_id=1, chord=chord)
    result = _executor(inputs).execute(action, _snapshot(FakeHandle()))
    assert inputs.chords == []
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == detail
    assert chord not in str(result.to_dict())


def test_blocked_chord_with_approval_is_sent_and_records_the_decision(fast_clock: Any) -> None:
    inputs = FakeInput()
    action = PhysicalAction(verb="press", element_id=1, chord="alt+f4", approval=APPROVAL)
    result = _executor(inputs).execute(action, _snapshot(FakeHandle()))
    assert inputs.chords == ["alt+f4"]
    assert result.evidence["decision_id"] == "d-7"


def test_foreground_that_cannot_be_acquired_is_focus_lost(fast_clock: Any) -> None:
    inputs = FakeInput(foreground=0x99, acquire_fails=True)
    action = PhysicalAction(verb="press", element_id=1, chord="ctrl+s")
    result = _executor(inputs).execute(action, _snapshot(FakeHandle()))
    assert inputs.chords == []
    assert result.code is ErrorCode.FOCUS_LOST
    assert result.evidence["detail"] == "foreground_refused"


def test_partially_injected_chord_is_uncertain_with_counts(fast_clock: Any) -> None:
    inputs = FakeInput(fail_chunk=1)
    action = PhysicalAction(verb="press", element_id=1, chord="ctrl+s")
    result = _executor(inputs).execute(action, _snapshot(FakeHandle()))
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.code is ErrorCode.VERIFY_FAILED
    assert result.evidence == {
        "reason": "partial_injection",
        "phase": "call",
        "foreground": "already",
        "inserted": 3,
        "sent": 16,
    }
    assert result.steps[-1].outcome == "failed"


# ---- type -------------------------------------------------------------------------------------


def test_type_sends_chunks_and_verifies_through_the_value_pattern(fast_clock: Any) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle)
    result = _executor(inputs).execute(_type("hello world, café"), _snapshot(handle, role="Edit"))
    assert inputs.bursts == ["text:8", "text:8", "text:1"]
    assert handle.value == "hello world, café"
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert result.steps[-1].label == "keyboard.type"
    assert result.evidence == {"foreground": "already", "chunks": 3, "chars": 17}


def test_type_brings_the_window_forward_first(fast_clock: Any) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle, foreground=0x99, acquire_procedure="direct")
    result = _executor(inputs).execute(_type("hi"), _snapshot(handle, role="Edit"))
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert result.evidence["foreground"] == "direct"


def test_type_into_a_field_already_holding_the_text_sends_nothing(fast_clock: Any) -> None:
    handle = _edit("hello")
    inputs = FakeInput(sink=handle)
    result = _executor(inputs).execute(_type("hello"), _snapshot(handle, role="Edit"))
    assert inputs.bursts == []
    assert result.skipped is True


def test_type_into_a_non_empty_field_is_refused_for_the_gate(fast_clock: Any) -> None:
    handle = _edit("Sent from my phone")
    inputs = FakeInput(sink=handle)
    result = _executor(inputs).execute(_type("hello"), _snapshot(handle, role="Edit"))
    assert inputs.bursts == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.GATE_PENDING_USER
    assert result.evidence == {"reason": "gate_pending_user", "detail": "field_not_empty"}


def test_type_into_a_read_only_field_is_not_actionable(fast_clock: Any) -> None:
    handle = _edit()
    handle.read_only = True
    result = _executor(FakeInput(sink=handle)).execute(_type("hi"), _snapshot(handle, role="Edit"))
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == "read_only"


def test_type_without_a_value_pattern_is_refused(fast_clock: Any) -> None:
    handle = FakeHandle(patterns=set())
    result = _executor(FakeInput(sink=handle)).execute(_type("hi"), _snapshot(handle, role="Edit"))
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == "unreadable_value"


def test_type_needs_focus_on_the_target_or_a_descendant(fast_clock: Any) -> None:
    handle = _edit()
    refused = _executor(FakeInput(sink=handle, focused=(9, 9))).execute(
        _type("hi"), _snapshot(handle, role="Edit")
    )
    assert refused.code is ErrorCode.FOCUS_LOST
    assert refused.evidence["detail"] == "target_not_focused"
    accepted = _executor(FakeInput(sink=handle, focused=CHILD_ID)).execute(
        _type("hi"), _snapshot(handle, role="Edit")
    )
    assert accepted.delivery is Delivery.DELIVERED_VERIFIED


def test_type_rejects_invalid_text_and_tabs_before_sending(fast_clock: Any) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle)
    bad = _executor(inputs).execute(_type("a\x1bb"), _snapshot(handle, role="Edit"))
    assert bad.code is ErrorCode.INVALID_TEXT
    assert bad.evidence == {"reason": "invalid_text"}
    tab = _executor(inputs).execute(_type("a\tb"), _snapshot(handle, role="Edit"))
    assert tab.code is ErrorCode.INVALID_TEXT
    assert tab.evidence["detail"] == "tab_not_typed"
    assert inputs.bursts == []


def test_type_newline_uses_the_binding_between_segments(fast_clock: Any) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle)
    action = _type("line one\nline two", newline=NewlineBinding("shift+enter"))
    result = _executor(inputs).execute(action, _snapshot(handle, role="Edit"))
    assert inputs.chords == ["shift+enter"]
    assert handle.value == "line one\nline two"
    assert result.delivery is Delivery.DELIVERED_VERIFIED


def test_type_newline_without_a_binding_is_invalid_text(fast_clock: Any) -> None:
    handle = _edit()
    result = _executor(FakeInput(sink=handle)).execute(
        _type("a\nb"), _snapshot(handle, role="Edit")
    )
    assert result.code is ErrorCode.INVALID_TEXT


def test_type_over_the_step_budget_is_refused(fast_clock: Any) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle)
    result = _executor(inputs).execute(
        _type("x" * (MAX_UNITS_PER_STEP + 1)), _snapshot(handle, role="Edit")
    )
    assert inputs.bursts == []
    assert result.code is ErrorCode.BUDGET_EXCEEDED
    assert result.evidence["detail"] == "typing_budget"


def test_foreground_stolen_between_chunks_stops_and_is_uncertain(fast_clock: Any) -> None:
    handle = _edit()

    def steal(inputs: FakeInput, index: int) -> None:
        if index == 2:
            inputs.foreground = 0x99
            inputs.foreground_name = "toast.exe"

    inputs = FakeInput(sink=handle, after_burst=steal)
    result = _executor(inputs).execute(_type("x" * 40), _snapshot(handle, role="Edit"))
    assert inputs.bursts == ["text:8", "text:8"]
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.code is ErrorCode.VERIFY_FAILED
    assert result.retry_safe is False
    assert result.evidence == {
        "reason": "focus_lost",
        "phase": "call",
        "foreground": "already",
        "chunks": 2,
        "foreground_process": "toast.exe",
    }


def test_focus_moved_to_another_field_between_chunks_stops(fast_clock: Any) -> None:
    handle = _edit()

    def advance(inputs: FakeInput, index: int) -> None:
        if index == 1:
            inputs.focused = (9, 9)

    inputs = FakeInput(sink=handle, after_burst=advance)
    result = _executor(inputs).execute(_type("x" * 20), _snapshot(handle, role="Edit"))
    assert inputs.bursts == ["text:8"]
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.evidence["reason"] == "focus_moved"


def test_modifier_pressed_between_chunks_stops(fast_clock: Any) -> None:
    handle = _edit()

    def hold(inputs: FakeInput, index: int) -> None:
        inputs.modifiers = ["shift"]

    inputs = FakeInput(sink=handle, after_burst=hold)
    result = _executor(inputs).execute(_type("x" * 20), _snapshot(handle, role="Edit"))
    assert inputs.bursts == ["text:8"]
    assert result.evidence["reason"] == "modifier_held"


def test_partial_injection_mid_text_is_uncertain(fast_clock: Any) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle, fail_chunk=2)
    result = _executor(inputs).execute(_type("x" * 20), _snapshot(handle, role="Edit"))
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.evidence["reason"] == "partial_injection"
    assert result.evidence["chunks"] == 1
    assert result.evidence["inserted"] == 3


def test_app_that_rewrites_the_text_is_verify_failed_with_a_token(fast_clock: Any) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle, transform=lambda v: v.replace("(", "()"))
    result = _executor(inputs).execute(_type("f(x"), _snapshot(handle, role="Edit"))
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.code is ErrorCode.VERIFY_FAILED
    assert result.evidence["detail"] == "readback_differs"
    assert "f(x" not in str(result.to_dict())


def test_field_with_a_max_length_reports_truncation(fast_clock: Any) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle, transform=lambda v: v[:10])
    result = _executor(inputs).execute(_type("x" * 20), _snapshot(handle, role="Edit"))
    assert result.code is ErrorCode.VERIFY_FAILED
    assert result.evidence["detail"] == "truncated"
    assert result.evidence["units_read"] == 10


def test_secure_field_evidence_carries_only_the_decision(fast_clock: Any) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle, transform=lambda v: v[:3])
    result = _executor(inputs).execute(
        _type("hunter2", approval=APPROVAL), _snapshot(handle, role="Edit", secure=True)
    )
    assert handle.value == "hun"
    assert result.code is ErrorCode.VERIFY_FAILED
    assert result.evidence == {
        "foreground": "already",
        "decision_id": "d-7",
        "detail": "truncated",
    }
    assert "hunter2" not in str(result.to_dict())


def test_secure_field_partial_injection_evidence_has_no_counts(fast_clock: Any) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle, fail_chunk=2)
    result = _executor(inputs).execute(
        _type("hunter2hunter2", approval=APPROVAL), _snapshot(handle, role="Edit", secure=True)
    )
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.evidence == {
        "reason": "partial_injection",
        "phase": "call",
        "foreground": "already",
        "decision_id": "d-7",
    }


def test_approval_on_a_non_secure_field_is_refused(fast_clock: Any) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle)
    result = _executor(inputs).execute(
        _type("hello", approval=APPROVAL), _snapshot(handle, role="Edit")
    )
    assert inputs.bursts == []
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "approval_mismatch"


def test_type_never_clicks_to_obtain_focus(fast_clock: Any) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle, focused=(9, 9))
    _executor(inputs).execute(_type("hi"), _snapshot(handle, role="Edit"))
    assert inputs.clicks == []
    assert inputs.moves == []


# ---- Windows ----------------------------------------------------------------------------------


def _notepad() -> tuple[subprocess.Popen[bytes], Snapshot, Element]:
    proc = subprocess.Popen(["notepad.exe"])  # noqa: S607 - fixed, well-known executable
    window = wait_window_for_pid(proc.pid)
    time.sleep(0.5)
    # The test runner's own window may cover the new Notepad: bring it forward first.
    acquire_foreground(window.hwnd)
    time.sleep(0.2)
    snapshot = WorldModel(UiaClient()).observe(window.hwnd, cap_s=5.0)
    target = next(e for e in snapshot.elements if e.role in ("Document", "Edit"))
    return proc, snapshot, target


@pytest.mark.windows
def test_click_then_type_into_notepad_round_trips() -> None:
    proc, snapshot, target = _notepad()
    try:
        executor = PhysicalExecutor()
        clicked = executor.execute(PhysicalAction(verb="click", element_id=target.id), snapshot)
        assert clicked.delivery is Delivery.DELIVERED_UNVERIFIED, clicked.to_dict()
        typed = executor.execute(
            PhysicalAction(verb="type", element_id=target.id, text="steer typed this: café"),
            snapshot,
        )
        assert typed.ok is True, typed.to_dict()
        assert typed.delivery is Delivery.DELIVERED_VERIFIED
        again = executor.execute(
            PhysicalAction(verb="type", element_id=target.id, text="steer typed this: café"),
            snapshot,
        )
        assert again.skipped is True, again.to_dict()
    finally:
        proc.terminate()


@pytest.mark.windows
def test_blocked_chord_on_notepad_is_refused_and_the_window_survives() -> None:
    proc, snapshot, target = _notepad()
    try:
        result = PhysicalExecutor().execute(
            PhysicalAction(verb="press", element_id=target.id, chord="alt+f4"), snapshot
        )
        assert result.code is ErrorCode.GATE_BLOCKED, result.to_dict()
        assert proc.poll() is None, "Notepad was closed"
    finally:
        proc.terminate()
