"""The physical executor (design 5.9 [v1.2]): click at a point, paced typing and chords through
SendInput, behind the same admission, readback and disposition contract as the semantic executor.

Pure tests drive it with the shared fake handle and a fake input port; the Windows tests type into
a throwaway Notepad.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

import pytest

import steerai.action.actionability as actionability_mod
import steerai.action.physical as physical_mod
import steerai.action.readback as readback_mod
from steerai.action.dispositions import Delivery
from steerai.action.execution import UiaBindings
from steerai.action.keyboard import InjectionError
from steerai.action.physical import (
    MAX_UNITS_PER_STEP,
    PhysicalAction,
    PhysicalExecutor,
    TypingPace,
)
from steerai.action.sanitize import NewlineBinding
from steerai.errors import ErrorCode
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.win_input import acquire_foreground
from steerai.probes.windows import is_hung, wait_window_for_pid
from steerai.world.elements import Element
from steerai.world.model import Snapshot, WorldModel
from tests.action.fakes import (
    APPROVAL,
    BOX,
    E_ACCESSDENIED,
    RPC_S_SERVER_UNAVAILABLE,
    TYPE_IDS,
    UIA_E_ELEMENTNOTAVAILABLE,
    UIA_E_ELEMENTNOTENABLED,
    UIA_E_TIMEOUT,
    UIA_VALUE,
    FakeHandle,
    FakeWindows,
    com_error,
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
    screen_box: tuple[int, int, int, int] = (0, 0, 1920, 1080)
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

    def screen_rect(self) -> tuple[int, int, int, int]:
        return self.screen_box

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

    def foreground_process(self, _hwnd: int) -> str:
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
        "cursor_at": [60, 30],
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
    inputs = FakeInput(cursor_override=(63, 30))
    result = _executor(inputs).execute(CLICK, _snapshot(FakeHandle()))
    assert inputs.clicks == []
    assert result.evidence["detail"] == "cursor_mismatch"


def test_cursor_one_pixel_off_inside_the_target_is_accepted(fast_clock: Any) -> None:
    # Measured 2026-11-04 (PX-39): absolute coordinates round through the virtual screen.
    inputs = FakeInput(cursor_override=(61, 31))
    result = _executor(inputs).execute(CLICK, _snapshot(FakeHandle()))
    assert inputs.clicks == [CENTRE]
    assert result.evidence["cursor_at"] == [61, 31]


def test_cursor_one_pixel_off_but_outside_a_thin_target_is_refused(fast_clock: Any) -> None:
    handle = FakeHandle(box=(10, 30, 110, 31))
    inputs = FakeInput(cursor_override=(60, 31))
    result = _executor(inputs).execute(CLICK, _snapshot(handle))
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


def test_element_below_the_physical_screen_is_clipped(fast_clock: Any) -> None:
    # Measured 2026-11-04: a 1200x900 logical Chrome window at 125 % runs past a 1080-pixel
    # screen; its composer sits at y 1101 and the cursor clamps to y 1079.
    handle = FakeHandle(box=(592, 1101, 803, 1124))
    inputs = FakeInput(window_box=(50, 50, 1550, 1152), screen_box=(0, 0, 1920, 1080))
    result = _executor(inputs).execute(CLICK, _snapshot(handle))
    assert inputs.moves == []
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == "clipped"


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


# ---- PX rows without a direct test, boundaries and failure phases (tester, 2026-11-04) --------


def _count_acquires(inputs: FakeInput) -> list[int]:
    """Record every `acquire_foreground` call (the ALT tap lives behind it)."""
    calls: list[int] = []
    original = inputs.acquire_foreground

    def counting(hwnd: int) -> str:
        calls.append(hwnd)
        return original(hwnd)

    inputs.acquire_foreground = counting  # type: ignore[method-assign]
    return calls


def _record_sleeps(monkeypatch: pytest.MonkeyPatch, now: dict[str, float]) -> list[float]:
    """Re-install the fast clock so every sleep it receives is recorded, in order."""
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += seconds

    fake = SimpleNamespace(monotonic=lambda: now["t"], sleep=sleep, perf_counter=lambda: now["t"])
    for module in (readback_mod, actionability_mod, physical_mod):
        monkeypatch.setattr(module, "time", fake)
    return sleeps


PRESS = PhysicalAction(verb="press", element_id=1, chord="ctrl+s")


@pytest.mark.parametrize(
    ("inputs", "detail"),
    [
        (FakeInput(foreground=0x99, modifiers=["alt"]), "modifier_held"),
        (FakeInput(foreground=0x99, buttons=["left"]), "button_held"),
    ],
)
def test_held_keys_are_refused_before_the_foreground_is_touched(
    fast_clock: Any, inputs: FakeInput, detail: str
) -> None:
    # PX-23: the ALT tap would compose with whatever the user holds; hands are checked first.
    acquires = _count_acquires(inputs)
    result = _executor(inputs).execute(PRESS, _snapshot(FakeHandle()))
    assert acquires == []
    assert inputs.chords == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.FOCUS_LOST
    assert result.evidence == {"reason": "focus_lost", "detail": detail}


def test_held_modifier_refuses_type_before_the_foreground_is_touched(fast_clock: Any) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle, foreground=0x99, modifiers=["ctrl"])
    acquires = _count_acquires(inputs)
    result = _executor(inputs).execute(_type("hi"), _snapshot(handle, role="Edit"))
    assert acquires == []
    assert inputs.bursts == []
    assert result.evidence["detail"] == "modifier_held"


def test_modifier_pressed_while_the_window_came_forward_stops_the_chord(fast_clock: Any) -> None:
    # PX-23: hands are re-checked after the foreground procedure, before the chord goes out.
    inputs = FakeInput(foreground=0x99)

    def acquire(hwnd: int) -> str:
        inputs.foreground = hwnd
        inputs.modifiers = ["shift"]
        return "alt-tap"

    inputs.acquire_foreground = acquire  # type: ignore[method-assign]
    result = _executor(inputs).execute(PRESS, _snapshot(FakeHandle()))
    assert inputs.chords == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.FOCUS_LOST
    assert result.evidence["detail"] == "modifier_held"


def test_chord_is_sent_once_when_the_postcondition_never_holds(fast_clock: Any) -> None:
    # PX-26: the readback times out; the chord is never re-sent to "help" it along.
    inputs = FakeInput()
    action = PhysicalAction(verb="press", element_id=1, chord="ctrl+s", postcondition=lambda: False)
    result = _executor(inputs).execute(action, _snapshot(FakeHandle()))
    assert inputs.chords == ["ctrl+s"]
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.code is ErrorCode.VERIFY_TIMEOUT
    assert result.retry_safe is False
    assert fast_clock["t"] >= 0.4, "the poll ran to its deadline"


def test_click_is_sent_once_when_the_postcondition_never_holds(fast_clock: Any) -> None:
    inputs = FakeInput()
    action = PhysicalAction(verb="click", element_id=1, postcondition=lambda: False)
    result = _executor(inputs).execute(action, _snapshot(FakeHandle()))
    assert inputs.clicks == [CENTRE]
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.code is ErrorCode.VERIFY_TIMEOUT


def test_type_does_not_accept_a_caller_postcondition() -> None:
    # `type` reads its own value back (design 5.9 [v1.1] SX-04); a caller predicate on it would
    # be sampled before the action and then never again, so it must be rejected at construction.
    with pytest.raises(ValueError, match="postcondition"):
        PhysicalAction(verb="type", element_id=1, text="x", postcondition=lambda: False)


@pytest.mark.parametrize(
    ("dx", "dy", "waits"),
    [
        (4, 0, True),
        (0, 4, True),
        (4, 4, True),
        (-4, -4, True),
        (5, 0, False),
        (0, 5, False),
        (-5, 0, False),
    ],
)
def test_double_click_distance_boundary(fast_clock: Any, dx: int, dy: int, waits: bool) -> None:
    # PX-03: SM_CXDOUBLECLK/2 = 4 px; at the limit it is a double click, one past it is not.
    inputs = FakeInput(double_click_s=0.5, double_click_px=4)
    executor = _executor(inputs)
    executor.execute(CLICK, _snapshot(FakeHandle()))
    first = fast_clock["t"]
    box = (BOX[0] + dx, BOX[1] + dy, BOX[2] + dx, BOX[3] + dy)
    executor.execute(CLICK, _snapshot(FakeHandle(box=box)))
    assert len(inputs.clicks) == 2
    assert (fast_clock["t"] - first >= 0.5) is waits


def test_second_click_after_the_double_click_time_does_not_wait(fast_clock: Any) -> None:
    inputs = FakeInput(double_click_s=0.5)
    executor = _executor(inputs)
    executor.execute(CLICK, _snapshot(FakeHandle()))
    fast_clock["t"] += 0.5
    first = fast_clock["t"]
    executor.execute(CLICK, _snapshot(FakeHandle()))
    assert fast_clock["t"] - first < 0.1


def test_zero_double_click_time_never_waits(fast_clock: Any) -> None:
    inputs = FakeInput(double_click_s=0.0)
    executor = _executor(inputs)
    executor.execute(CLICK, _snapshot(FakeHandle()))
    first = fast_clock["t"]
    executor.execute(CLICK, _snapshot(FakeHandle()))
    assert fast_clock["t"] - first < 0.1
    assert len(inputs.clicks) == 2


def test_element_replaced_under_the_cursor_before_the_button_goes_down(fast_clock: Any) -> None:
    # PX-08: admission hit the target; by the last look a popup owns the point.
    inputs = FakeInput(hits=[TARGET_ID, (9, 9)])
    result = _executor(inputs).execute(CLICK, _snapshot(FakeHandle()))
    assert inputs.moves == [CENTRE]
    assert inputs.clicks == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == "target_moved"


def test_window_covered_after_the_cursor_arrived_is_not_clicked(fast_clock: Any) -> None:
    inputs = FakeInput()

    def moved(x: int, y: int) -> None:
        inputs.moves.append((x, y))
        inputs.root_at = 0x99

    inputs.move_cursor = moved  # type: ignore[method-assign]
    result = _executor(inputs).execute(CLICK, _snapshot(FakeHandle()))
    assert inputs.clicks == []
    assert result.evidence["detail"] == "target_moved"


def test_window_rect_failure_before_the_click_is_perception_timeout(fast_clock: Any) -> None:
    inputs = FakeInput()

    def broken(_hwnd: int) -> tuple[int, int, int, int]:
        raise OSError("GetWindowRect failed for hwnd 0x10: error 1400")

    inputs.window_rect = broken  # type: ignore[method-assign]
    result = _executor(inputs).execute(CLICK, _snapshot(FakeHandle()))
    assert inputs.moves == []
    assert inputs.clicks == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.retry_safe is True
    assert result.code is ErrorCode.PERCEPTION_TIMEOUT
    assert result.evidence == {"reason": "perception_timeout"}


@pytest.mark.parametrize(
    ("hresult", "code"),
    [
        (UIA_E_ELEMENTNOTAVAILABLE, ErrorCode.ELEMENT_NOT_FOUND),
        (E_ACCESSDENIED, ErrorCode.ELEMENT_NOT_FOUND),
        (UIA_E_ELEMENTNOTENABLED, ErrorCode.ELEMENT_NOT_ACTIONABLE),
        (RPC_S_SERVER_UNAVAILABLE, ErrorCode.APP_CRASHED),
        (UIA_E_TIMEOUT, ErrorCode.PERCEPTION_TIMEOUT),
    ],
)
def test_com_error_from_the_hit_test_is_classified_and_nothing_is_sent(
    fast_clock: Any, hresult: int, code: ErrorCode
) -> None:
    inputs = FakeInput()

    def failing(_x: int, _y: int) -> tuple[int, ...]:
        raise com_error(hresult)

    inputs.runtime_id_at = failing  # type: ignore[method-assign]
    result = _executor(inputs).execute(CLICK, _snapshot(FakeHandle()))
    assert inputs.clicks == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is code
    assert result.evidence == {"reason": code.value, "hresult": f"0x{hresult:08X}"}


def test_programming_error_before_anything_is_sent_propagates(fast_clock: Any) -> None:
    inputs = FakeInput()

    def bug(_x: int, _y: int) -> tuple[int, ...]:
        raise TypeError("boom")

    inputs.runtime_id_at = bug  # type: ignore[method-assign]
    with pytest.raises(TypeError, match="boom"):
        _executor(inputs).execute(CLICK, _snapshot(FakeHandle()))


def test_com_error_after_the_click_is_uncertain_in_the_readback_phase(fast_clock: Any) -> None:
    inputs = FakeInput()
    calls = {"n": 0}

    def hits(_x: int, _y: int) -> tuple[int, ...]:
        calls["n"] += 1
        if calls["n"] == 3:  # admission, last look, then the `hit_after` read
            raise com_error(UIA_E_ELEMENTNOTAVAILABLE)
        return TARGET_ID

    inputs.runtime_id_at = hits  # type: ignore[method-assign]
    result = _executor(inputs).execute(CLICK, _snapshot(FakeHandle()))
    assert inputs.clicks == [CENTRE]
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.retry_safe is False
    assert result.code is ErrorCode.VERIFY_FAILED
    assert result.evidence == {
        "reason": "element_not_found",
        "phase": "readback",
        "hresult": "0x80040201",
    }


def test_hit_with_an_empty_runtime_id_is_never_accepted(fast_clock: Any) -> None:
    # ElementFromPoint answered nothing and a snapshot child also lacks an id: () must not match.
    handle = FakeHandle()
    snapshot = _snapshot(handle)
    snapshot.elements[2] = replace(snapshot.elements[2], runtime_id=())
    inputs = FakeInput(hits=[()])
    result = _executor(inputs).execute(CLICK, snapshot)
    assert inputs.clicks == []
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == "covered_in_window"


def test_target_without_a_runtime_id_accepts_only_identified_descendants(fast_clock: Any) -> None:
    handle = FakeHandle()
    snapshot = _snapshot(handle)
    snapshot.elements[1] = replace(snapshot.elements[1], runtime_id=())
    refused = _executor(FakeInput(hits=[()])).execute(CLICK, snapshot)
    assert refused.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert refused.evidence["detail"] == "covered_in_window"
    accepted = _executor(FakeInput(hits=[CHILD_ID])).execute(CLICK, snapshot)
    assert accepted.delivery is Delivery.DELIVERED_UNVERIFIED


def test_ancestor_without_a_runtime_id_does_not_turn_a_miss_into_hit_test_miss(
    fast_clock: Any,
) -> None:
    handle = FakeHandle()
    snapshot = _snapshot(handle)
    snapshot.elements[0] = replace(snapshot.elements[0], runtime_id=())
    result = _executor(FakeInput(hits=[()])).execute(CLICK, snapshot)
    assert result.evidence["detail"] == "covered_in_window"


def test_type_with_no_focused_element_is_refused(fast_clock: Any) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle, focused=())
    result = _executor(inputs).execute(_type("hi"), _snapshot(handle, role="Edit"))
    assert inputs.bursts == []
    assert result.code is ErrorCode.FOCUS_LOST
    assert result.evidence["detail"] == "target_not_focused"


@pytest.mark.parametrize(("text", "expected"), [("hi", []), ("x" * 20, [0.02, 0.02])])
def test_chunk_delay_sleeps_between_bursts_and_never_after_the_last(
    fast_clock: Any, monkeypatch: pytest.MonkeyPatch, text: str, expected: list[float]
) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle)
    sleeps = _record_sleeps(monkeypatch, fast_clock)
    executor = PhysicalExecutor(
        uia=UiaBindings(control_types={v: k for k, v in TYPE_IDS.items()}, interfaces={}),
        windows=FakeWindows(),
        clock=lambda: 0.0,
        inputs=inputs,
        pace=TypingPace(chunk_delay_s=0.02),
    )
    result = executor.execute(_type(text), _snapshot(handle, role="Edit"))
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert [s for s in sleeps if s == 0.02] == expected


def test_default_pace_never_sleeps_between_bursts(
    fast_clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle)
    sleeps = _record_sleeps(monkeypatch, fast_clock)
    result = _executor(inputs).execute(_type("x" * 20), _snapshot(handle, role="Edit"))
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert 0.0 not in sleeps
    assert len(inputs.bursts) == 3


@pytest.mark.parametrize(
    "kwargs", [{"chunk_delay_s": -0.01}, {"max_units_per_step": 0}, {"max_units_per_step": -1}]
)
def test_typing_pace_rejects_out_of_range_values(kwargs: dict[str, Any]) -> None:
    # A negative delay reaches `time.sleep` after the first burst and surfaces as an uncertain
    # delivery; a non-positive cap refuses every text. Both are configuration errors.
    with pytest.raises(ValueError, match=r"chunk_delay_s|max_units_per_step"):
        TypingPace(**kwargs)


def test_text_exactly_at_the_budget_is_typed(fast_clock: Any) -> None:
    # PX-29 boundary: the limit itself is allowed, the limit plus one is refused (tested above).
    handle = _edit()
    inputs = FakeInput(sink=handle)
    result = _executor(inputs).execute(
        _type("x" * MAX_UNITS_PER_STEP), _snapshot(handle, role="Edit")
    )
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert len(inputs.bursts) == -(-MAX_UNITS_PER_STEP // 8), "ceil(units / 8) bursts"
    assert result.evidence["chars"] == MAX_UNITS_PER_STEP


def test_budget_counts_utf16_units_not_characters(fast_clock: Any) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle)
    executor = _executor(inputs)
    within = executor.execute(
        _type("\U0001f600" * (MAX_UNITS_PER_STEP // 2)), _snapshot(handle, role="Edit")
    )
    assert within.delivery is Delivery.DELIVERED_VERIFIED
    assert within.evidence["chars"] == MAX_UNITS_PER_STEP
    handle.value = ""
    over = executor.execute(
        _type("\U0001f600" * (MAX_UNITS_PER_STEP // 2 + 1)), _snapshot(handle, role="Edit")
    )
    assert over.code is ErrorCode.BUDGET_EXCEEDED
    assert over.evidence["detail"] == "typing_budget"


@dataclass
class _FlickerHandle(FakeHandle):
    """Once text is in, the value reads differently on every sample (a live formatter)."""

    reads: int = 0

    def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802
        inner = super().GetCurrentPattern(pattern_id)
        if inner is None:
            return None
        outer = self

        class _Flicker:
            def __getattr__(self, name: str) -> Any:
                return getattr(inner, name)

            @property
            def CurrentValue(self) -> str | None:  # noqa: N802
                outer.reads += 1
                if not outer.value:
                    return outer.value
                return outer.value + ("~" if outer.reads % 2 else "~~")

        return _Flicker()


def test_readback_that_never_settles_is_verify_timeout(fast_clock: Any) -> None:
    handle = _FlickerHandle(patterns={UIA_VALUE})
    inputs = FakeInput(sink=handle)
    result = _executor(inputs).execute(_type("hello"), _snapshot(handle, role="Edit"))
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.code is ErrorCode.VERIFY_TIMEOUT
    assert result.evidence == {"foreground": "already", "chunks": 1, "chars": 5}
    assert handle.reads >= 9, "first sample plus the eight bounded steps"


def test_app_that_appends_after_the_last_chunk_is_readback_differs(fast_clock: Any) -> None:
    # Autocomplete fires once the whole word is in: the value is a superset, not a prefix.
    handle = _edit()
    inputs = FakeInput(sink=handle, transform=lambda v: v + ".com" if v == "steer" else v)
    result = _executor(inputs).execute(_type("steer"), _snapshot(handle, role="Edit"))
    assert handle.value == "steer.com"
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.code is ErrorCode.VERIFY_FAILED
    assert result.evidence["detail"] == "readback_differs"
    assert "units_read" not in result.evidence


@dataclass
class _LateHandle(FakeHandle):
    """Newline chords the app shows only from the `visible_after`-th value read on."""

    pending: list[int] = field(default_factory=list)
    visible_after: int = 3
    reads: int = 0

    def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802
        inner = super().GetCurrentPattern(pattern_id)
        if inner is None:
            return None
        outer = self

        class _Late:
            def __getattr__(self, name: str) -> Any:
                return getattr(inner, name)

            @property
            def CurrentValue(self) -> str | None:  # noqa: N802
                outer.reads += 1
                if outer.reads >= outer.visible_after and outer.pending:
                    value = outer.value or ""
                    for position in reversed(outer.pending):
                        value = value[:position] + "\n" + value[position:]
                    outer.value, outer.pending = value, []
                return outer.value

        return _Late()


@dataclass
class _SlowChordInput(FakeInput):
    """The chord goes out at once; the app applies it later (see `_LateHandle`)."""

    def press(self, chord: str, _approval: Any = None) -> None:
        assert isinstance(self.sink, _LateHandle)
        self.chords.append(chord)
        self.sink.pending.append(len(self.sink.value or ""))
        self._burst(f"chord:{chord}", "")


def test_newline_chord_the_app_consumes_slowly_still_verifies(fast_clock: Any) -> None:
    handle = _LateHandle(patterns={UIA_VALUE}, visible_after=3)
    inputs = _SlowChordInput(sink=handle)
    action = _type("line one\nline two", newline=NewlineBinding("shift+enter"))
    result = _executor(inputs).execute(action, _snapshot(handle, role="Edit"))
    assert inputs.chords == ["shift+enter"]
    assert handle.value == "line one\nline two"
    assert result.delivery is Delivery.DELIVERED_VERIFIED
    assert result.evidence == {"foreground": "already", "chunks": 3, "chars": 17}
    assert fast_clock["t"] < 0.4, "verified before the readback deadline"


def test_newline_chord_the_app_never_shows_is_readback_differs(fast_clock: Any) -> None:
    handle = _LateHandle(patterns={UIA_VALUE}, visible_after=100)
    inputs = _SlowChordInput(sink=handle)
    action = _type("line one\nline two", newline=NewlineBinding("shift+enter"))
    result = _executor(inputs).execute(action, _snapshot(handle, role="Edit"))
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.code is ErrorCode.VERIFY_FAILED
    assert result.evidence["detail"] == "readback_differs"


def test_secure_field_focus_loss_evidence_has_no_chunk_count(fast_clock: Any) -> None:
    # PX-04 + PX-28: the stop reason and the stealing process name are tokens; the count is not.
    handle = _edit()

    def steal(inputs: FakeInput, index: int) -> None:
        if index == 1:
            inputs.foreground = 0x99
            inputs.foreground_name = "toast.exe"

    inputs = FakeInput(sink=handle, after_burst=steal)
    result = _executor(inputs).execute(
        _type("hunter2hunter2", approval=APPROVAL), _snapshot(handle, role="Edit", secure=True)
    )
    assert inputs.bursts == ["text:8"]
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.evidence == {
        "reason": "focus_lost",
        "phase": "call",
        "foreground": "already",
        "decision_id": "d-7",
        "foreground_process": "toast.exe",
    }
    assert "hunter2" not in str(result.to_dict())


@pytest.mark.parametrize("binding", ["alt+f4", "win+shift+e", "enter", "return"])
def test_newline_binding_that_is_blocked_or_bare_enter_is_invalid_text(
    fast_clock: Any, binding: str
) -> None:
    handle = _edit()
    inputs = FakeInput(sink=handle)
    action = _type("a\nb", newline=NewlineBinding(binding))
    result = _executor(inputs).execute(action, _snapshot(handle, role="Edit"))
    assert inputs.bursts == []
    assert inputs.chords == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.INVALID_TEXT
    assert result.evidence == {"reason": "invalid_text"}
    assert binding not in str(result.to_dict())


@pytest.mark.parametrize("binding", ["ctrl+v", "shift+insert", "win+v"])
def test_newline_binding_that_routes_to_the_clipboard_is_refused_before_typing(
    fast_clock: Any, binding: str
) -> None:
    # A routed chord is never injected raw (design 5.9 [v1.0]); `keyboard.press` would refuse
    # it, but only after the first text chunk had gone out.
    handle = _edit()
    inputs = FakeInput(sink=handle)
    action = _type("a\nb", newline=NewlineBinding(binding))
    result = _executor(inputs).execute(action, _snapshot(handle, role="Edit"))
    assert inputs.bursts == []
    assert inputs.chords == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.INVALID_TEXT
    assert result.evidence == {"reason": "invalid_text"}


def test_blocker_before_the_first_burst_is_a_refusal_with_nothing_sent(
    fast_clock: Any,
) -> None:
    handle = _edit()

    class _AltWhileComingForward(FakeInput):
        def acquire_foreground(self, hwnd: int) -> str:
            self.foreground = hwnd
            self.modifiers = ["alt"]
            return "alt-tap"

    inputs = _AltWhileComingForward(sink=handle)
    result = _executor(inputs).execute(_type("hello"), _snapshot(handle, role="Edit"))
    assert inputs.bursts == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.FOCUS_LOST
    assert result.retry_safe is True
    assert result.steps == []
    assert result.evidence == {"reason": "focus_lost", "detail": "modifier_held"}


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


@pytest.mark.windows
def test_notepad_document_owns_the_point_and_the_focus_after_a_click() -> None:
    # PX-02/PX-12/PX-18 live: measured 2026-11-04 on Win10 Notepad, the Document's RuntimeId is
    # what `ElementFromPoint` and `GetFocusedElement` answer once Steer has clicked into it.
    proc, snapshot, target = _notepad()
    try:
        assert is_hung(snapshot.hwnd) is False
        assert target.runtime_id, "the Notepad text area has a RuntimeId"
        executor = PhysicalExecutor()
        clicked = executor.execute(PhysicalAction(verb="click", element_id=target.id), snapshot)
        assert clicked.delivery is Delivery.DELIVERED_UNVERIFIED, clicked.to_dict()
        assert clicked.evidence["hit_after"] == "target"
        x, y = clicked.evidence["clicked_at"]
        client = UiaClient()
        assert client.runtime_id_at(x, y) == target.runtime_id
        assert client.focused_runtime_id() == target.runtime_id
    finally:
        proc.terminate()
