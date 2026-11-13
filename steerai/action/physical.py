"""The physical executor (design 5.9 [v1.2]): click at a point, paced typing and chords through
SendInput, behind the same admission and disposition contract as the semantic executor.

Admission (after the shared checks in `execution.Admission`): the process is Per-Monitor-V2
aware, the input desktop is the default one, no accessibility key filter is active, the target
is not Steer's own process, the window is not minimized or hung, UIPI allows input, the live
rectangle is on screen, stable over two samples and at least partly inside the window, the
Win32 root window and the UIA element under the click point belong to the target, and no modifier
key or mouse button is physically held. A refusal sends nothing and is `not_delivered`.

`click` moves the cursor, confirms it landed and takes a last look at the target before the
button goes down; a second click within the system double-click time and distance waits the
window out so Windows never synthesises a double click. `type` needs a readable value
(ValuePattern): equal → already in state; non-empty → refused for the gate (Steer never clears
text it did not author); empty → the window is brought forward, focus must be on the target, and
text goes in `chunk_text` chunks with the foreground, focus and modifier state re-checked around
every burst (a mismatch mid-text is `delivery_uncertain`, never resumed). `press` refuses blocked
and routed chords before sending. SendInput's return value is never delivery evidence.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from steerai.action import keyboard
from steerai.action.actionability import (
    centre_of,
    integrity_allows_input,
    process_integrity_level,
    sample_stable_rect,
)
from steerai.action.dispositions import ActionResult, Delivery, Step
from steerai.action.execution import (
    UIA_VALUE_PATTERN,
    Admission,
    Call,
    LiveWindows,
    RefusalError,
    UiaBindings,
    WindowState,
    classify,
)
from steerai.action.keyboard import GateApproval, InjectionError
from steerai.action.readback import Readback, poll_state, poll_until
from steerai.action.sanitize import (
    NewlineBinding,
    field_text,
    normalise_newlines,
    sanitize_text,
)
from steerai.action.tree import TREE_ITEM, own_descendants, own_hit_ids, own_row
from steerai.debug_trace import DebugSink, NullDebugSink
from steerai.errors import ErrorCode, SteerError
from steerai.probes import win_input, windows
from steerai.probes.uia_snapshot import UiaClient
from steerai.world.elements import Box, Element
from steerai.world.model import Snapshot

VERBS: frozenset[str] = frozenset({"click", "type", "press"})
TEXT_ROLES: frozenset[str] = frozenset({"Edit", "ComboBox", "Document"})
"""Roles a chord may land on: the target itself, or such a descendant of it (5.9 [v1.7])."""
POPUP_OPTION_ROLES: frozenset[str] = frozenset({"ListItem", "TreeItem", "MenuItem", "DataItem"})
"""The UIA roles of a popup's options, per Core-AAM: option maps to ListItem, menuitem to
MenuItem, gridcell and row to DataItem, treeitem to TreeItem. A combobox that moves its
`aria-activedescendant` keeps DOM focus in the field, but Chrome reports the active option as
the UIA focus, and Enter would accept that option (D-151). Hyperlink is not here: a focused link
is the page-stole-focus case (SE-01)."""
CURSOR_TOLERANCE_PX = 1
MAX_UNITS_PER_STEP = 500
"""A ceiling, not a budget: typing time is additive to the step. Measured 2026-11-04 (probe
typing-pace, `keyboard.type_text` alone): about 1 ms/unit in Chrome and 3 ms/unit in Notepad,
so 500 units cost 0.5 to 1.5 s before the executor's per-burst checks, which are unmeasured.
Longer text is the orchestrator's to split across steps."""
DEFAULT_CHUNK_DELAY_S = 0.0
"""Measured 2026-11-04: no dropped units at 0 ms on Notepad or a Chrome textarea."""
_TAB = "\t"


@dataclass(frozen=True, slots=True)
class TypingPace:
    chunk_delay_s: float = DEFAULT_CHUNK_DELAY_S
    max_units_per_step: int = MAX_UNITS_PER_STEP

    def __post_init__(self) -> None:
        if self.chunk_delay_s < 0:
            raise ValueError("chunk_delay_s must be >= 0")
        if self.max_units_per_step <= 0:
            raise ValueError("max_units_per_step must be > 0")


DEFAULT_PACE = TypingPace()


@dataclass(frozen=True, slots=True)
class _TypePlan:
    """A validated `type`: the value pattern to read back, the wanted value and the bursts."""

    pattern: Any  # IUIAutomationValuePattern; comtypes has no stubs
    read: Callable[[], str]
    wanted: str
    units: int
    bursts: list[_Burst]


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class InputPort(Protocol):
    """The Win32 side of the executor; `LiveInput` is the real one, tests inject a fake."""

    def dpi_aware(self) -> bool: ...
    def input_desktop_name(self) -> str: ...
    def accessibility_filters_active(self) -> bool: ...
    def own_pid(self) -> int: ...
    def is_hung(self, hwnd: int) -> bool: ...
    def integrity_level(self, pid: int) -> int | None: ...
    def caller_integrity_level(self) -> int: ...
    def window_rect(self, hwnd: int) -> Box: ...
    def screen_rect(self) -> Box: ...
    def root_hwnd_at(self, x: int, y: int) -> int: ...
    def runtime_id_at(self, x: int, y: int) -> tuple[int, ...]: ...
    def physical_modifiers_down(self) -> list[str]: ...
    def physical_buttons_down(self) -> list[str]: ...
    def foreground_hwnd(self) -> int: ...
    def foreground_process(self, hwnd: int) -> str: ...
    def window_pid(self, hwnd: int) -> int: ...
    def in_menu_mode(self, hwnd: int) -> bool: ...
    def acquire_foreground(self, hwnd: int) -> str: ...
    def focused_runtime_id(self) -> tuple[int, ...]: ...
    def double_click_time_s(self) -> float: ...
    def double_click_size_px(self) -> int: ...
    def move_cursor(self, x: int, y: int) -> None: ...
    def cursor_pos(self) -> tuple[int, int]: ...
    def click_at(self, x: int, y: int) -> None: ...
    def type_text(self, text: str) -> None: ...
    def press(self, chord: str, approval: GateApproval | None = None) -> None: ...


class LiveInput:
    """`InputPort` over win_input, keyboard, actionability and the UIA client."""

    def __init__(self, client: UiaClient) -> None:
        self._client = client

    def dpi_aware(self) -> bool:
        return win_input.dpi_context_is_per_monitor_v2()

    def input_desktop_name(self) -> str:
        return windows.input_desktop_name()

    def accessibility_filters_active(self) -> bool:
        return win_input.accessibility_filters_active()

    def own_pid(self) -> int:
        return os.getpid()

    def is_hung(self, hwnd: int) -> bool:
        return windows.is_hung(hwnd)

    def integrity_level(self, pid: int) -> int | None:
        return process_integrity_level(pid)

    def caller_integrity_level(self) -> int:
        level = process_integrity_level(os.getpid())
        if level is None:
            raise OSError("cannot read this process's own integrity level")
        return level

    def window_rect(self, hwnd: int) -> Box:
        return win_input.window_rect(hwnd)

    def screen_rect(self) -> Box:
        return win_input.virtual_screen_rect()

    def root_hwnd_at(self, x: int, y: int) -> int:
        return win_input.root_hwnd_at(x, y)

    def runtime_id_at(self, x: int, y: int) -> tuple[int, ...]:
        return self._client.runtime_id_at(x, y)

    def physical_modifiers_down(self) -> list[str]:
        return win_input.physical_modifiers_down()

    def physical_buttons_down(self) -> list[str]:
        return win_input.physical_buttons_down()

    def foreground_hwnd(self) -> int:
        return win_input.foreground_hwnd()

    def foreground_process(self, hwnd: int) -> str:
        return windows.process_name(windows.window_pid(hwnd)) if hwnd else ""

    def window_pid(self, hwnd: int) -> int:
        return windows.window_pid(hwnd)

    def in_menu_mode(self, hwnd: int) -> bool:
        return win_input.in_menu_mode(hwnd)

    def acquire_foreground(self, hwnd: int) -> str:
        return win_input.acquire_foreground(hwnd)

    def focused_runtime_id(self) -> tuple[int, ...]:
        return self._client.focused_runtime_id()

    def double_click_time_s(self) -> float:
        return win_input.double_click_time_s()

    def double_click_size_px(self) -> int:
        return win_input.double_click_size_px()

    def move_cursor(self, x: int, y: int) -> None:
        win_input.move_cursor(x, y)

    def cursor_pos(self) -> tuple[int, int]:
        return win_input.cursor_pos()

    def click_at(self, x: int, y: int) -> None:
        win_input.click_at(x, y)

    def type_text(self, text: str) -> None:
        keyboard.type_text(text)

    def press(self, chord: str, approval: GateApproval | None = None) -> None:
        keyboard.press(chord, approval=approval)


@dataclass(frozen=True, slots=True)
class PhysicalAction:
    verb: str
    element_id: int
    text: str | None = field(default=None, repr=False)
    chord: str | None = None
    newline: NewlineBinding | None = None
    approval: GateApproval | None = None
    postcondition: Callable[[], bool] | None = None

    def __post_init__(self) -> None:
        if self.verb not in VERBS:
            raise ValueError(f"unknown verb {self.verb!r}")
        if (self.verb == "type") != (self.text is not None):
            raise ValueError("text goes with type and only with type")
        if (self.verb == "press") != (self.chord is not None):
            raise ValueError("chord goes with press and only with press")
        if self.newline is not None and self.verb != "type":
            raise ValueError("newline binding goes with type")
        if self.approval is not None and not isinstance(self.approval, GateApproval):
            raise TypeError("approval must be a GateApproval from the gate, not a flag")
        if self.postcondition is not None and self.verb == "type":
            raise ValueError("type reads its value back; it takes no postcondition")


@dataclass(frozen=True, slots=True)
class _Target:
    element: Element
    handle: Any  # live COM element; comtypes has no stubs
    point: tuple[int, int]
    visible: Box
    subtree: frozenset[tuple[int, ...]]
    """RuntimeIds of the element and its snapshot descendants: the hit-test acceptance set (a
    tree item's: itself and its plain descendants outside its child items, [v2.81] TE-01)."""
    ancestors: frozenset[tuple[int, ...]]
    row: int | None = None
    """A tree item's own-row height from its top ([v2.81]); None aims at the centre."""
    live: Box | None = None
    """The live rectangle the aim was taken from (the debug record only)."""


@dataclass(frozen=True, slots=True)
class _Burst:
    """One SendInput burst of a `type` action: a text chunk or the newline chord."""

    kind: str
    payload: str


def _plain_element(element: Element) -> dict[str, Any]:
    return {"id": element.id, "role": element.role, "name": element.name, "box": list(element.box)}


def _aim_record(snapshot: Snapshot, target: _Target, hit: tuple[int, ...]) -> dict[str, Any]:
    """Where the click went, from values already read (V2 step 1 packet §B): the element, its
    snapshot box, the live and visible rectangles, the row and the point (physical pixels),
    and what the one hit test after the click found, resolved in the snapshot (no new read)."""
    # () is no hit at all, never the first element read without a RuntimeId (#259 item 2)
    found = None if not hit else next((e for e in snapshot.elements if e.runtime_id == hit), None)
    return {
        "element": _plain_element(target.element),
        "live": None if target.live is None else list(target.live),
        "visible": list(target.visible),
        "row": target.row,
        "point": list(target.point),
        "hit": "not_in_snapshot" if found is None else _plain_element(found),
    }


def _neighbourhood(snapshot: Snapshot, element: Element) -> dict[str, Any]:
    """The clicked element's parent and own descendants, from the snapshot it was sent against."""
    parent = next((e for e in snapshot.elements if e.id == element.parent_id), None)
    return {
        "parent": None if parent is None else _plain_element(parent),
        "descendants": [_plain_element(e) for e in own_descendants(snapshot, element)],
    }


class PhysicalExecutor:
    def __init__(  # noqa: PLR0913 - every collaborator is injected so the executor runs pure in tests
        self,
        *,
        uia: UiaBindings | None = None,
        windows: WindowState | None = None,
        clock: Callable[[], float] = time.time,
        inputs: InputPort | None = None,
        pace: TypingPace = DEFAULT_PACE,
        blocker: Callable[[], str | None] | None = None,
        debug: DebugSink | None = None,
    ) -> None:
        self._blocker = blocker
        self._debug: DebugSink = NullDebugSink() if debug is None else debug
        self._uia = UiaBindings.load() if uia is None else uia
        self._admission = Admission(
            windows=LiveWindows() if windows is None else windows, bindings=self._uia, clock=clock
        )
        self._inputs: InputPort = LiveInput(UiaClient()) if inputs is None else inputs
        self._pace = pace
        self._last_click: tuple[float, tuple[int, int]] | None = None

    def execute(self, action: PhysicalAction, snapshot: Snapshot) -> ActionResult:
        call = Call(action.verb, "physical", blocker=self._blocker)
        try:
            target = self._admit(action, snapshot)
            call.secure = target.element.secure
            return _DISPATCH[action.verb](self, action, snapshot, target, call)
        except Exception as exc:
            result = classify(call, exc)
            if result is None:
                raise
            return result

    # ---- admission ---------------------------------------------------------------------------

    def _admit(self, action: PhysicalAction, snapshot: Snapshot) -> _Target:
        element, handle = self._admission.admit(action, snapshot)
        inputs = self._inputs
        if not inputs.dpi_aware():
            raise RefusalError(ErrorCode.PERCEPTION_DEGRADED, "dpi_context")
        if inputs.input_desktop_name() != "Default":
            raise RefusalError(ErrorCode.SECURE_DESKTOP, "secure_desktop")
        if inputs.accessibility_filters_active():
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "accessibility_filter")
        if snapshot.pid == inputs.own_pid():
            raise RefusalError(ErrorCode.GATE_BLOCKED, "self_target")
        if snapshot.minimized:
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "minimized")
        if inputs.is_hung(snapshot.hwnd):
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "hung")
        allowed = integrity_allows_input(
            caller_level=inputs.caller_integrity_level(),
            target_level=inputs.integrity_level(snapshot.pid),
        )
        if not allowed:
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "integrity_blocked")
        if bool(handle.CurrentIsOffscreen):
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "offscreen")
        live = sample_stable_rect(lambda: _rect(handle))
        visible = _visible_part(live, inputs.window_rect(snapshot.hwnd), inputs.screen_rect())
        row = own_row(snapshot, element)
        point = None if visible is None else _aim(live, visible, row)
        if visible is None or point is None:
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "clipped")
        target = _Target(
            element=element,
            handle=handle,
            point=point,
            visible=visible,
            subtree=(
                own_hit_ids(snapshot, element)
                if element.role == TREE_ITEM
                else _subtree_ids(snapshot, element)
            ),
            ancestors=_ancestor_ids(snapshot, element),
            row=row,
            live=live,
        )
        if action.verb != "press":
            # a chord lands on the focused element, never under the pointer
            self._hit_test(target, snapshot.hwnd)
        self._check_hands_off()
        return target

    def _hit_test(self, target: _Target, hwnd: int, *, moved_detail: str | None = None) -> None:
        """The Win32 root window and the UIA element under the point must be the target's."""
        inputs = self._inputs
        if inputs.root_hwnd_at(*target.point) != hwnd:
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, moved_detail or "covered")
        hit = inputs.runtime_id_at(*target.point)
        if hit in target.subtree:
            return
        if moved_detail is not None:
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, moved_detail)
        detail = "hit_test_miss" if hit in target.ancestors else "covered_in_window"
        raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, detail)

    def _check_hands_off(self) -> None:
        if self._inputs.physical_modifiers_down():
            raise RefusalError(ErrorCode.FOCUS_LOST, "modifier_held")
        if self._inputs.physical_buttons_down():
            raise RefusalError(ErrorCode.FOCUS_LOST, "button_held")

    def _refuse_menu_mode(self, hwnd: int) -> None:
        """Keys into a window in menu mode are mnemonics, not text (issue #38, SF-03)."""
        if self._inputs.in_menu_mode(hwnd):
            raise RefusalError(ErrorCode.FOCUS_LOST, "menu_mode")

    def _bring_forward(self, hwnd: int) -> str:
        try:
            procedure = self._inputs.acquire_foreground(hwnd)
        except OSError as exc:
            raise RefusalError(ErrorCode.FOCUS_LOST, "foreground_refused") from exc
        if self._inputs.foreground_hwnd() != hwnd:
            raise RefusalError(ErrorCode.FOCUS_LOST, "foreground_refused")
        return procedure

    # ---- click -------------------------------------------------------------------------------

    def _click(
        self, action: PhysicalAction, snapshot: Snapshot, target: _Target, call: Call
    ) -> ActionResult:
        inputs = self._inputs
        self._wait_out_double_click(target.point)
        evidence: dict[str, Any] = {
            "clicked_at": list(target.point),
            "foreground_was_target": inputs.foreground_hwnd() == snapshot.hwnd,
        }
        inputs.move_cursor(*target.point)
        cursor = inputs.cursor_pos()
        # Absolute SendInput coordinates round through the virtual screen (PX-39). Measured
        # 2026-11-04 on a single 125 % monitor: on-screen points land exactly; the one-pixel
        # tolerance covers the inverse mapping on other layouts (unmeasured). The cursor must
        # sit on the target: inside its visible rectangle and within tolerance of the point.
        if not _within(cursor, target.point, CURSOR_TOLERANCE_PX, target.visible):
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "cursor_mismatch")
        evidence["cursor_at"] = list(cursor)
        # Last look before the button goes down: still the same rectangle, still the target.
        live = _rect(target.handle)
        visible = _visible_part(live, inputs.window_rect(snapshot.hwnd), inputs.screen_rect())
        if visible is None or _aim(live, visible, target.row) != target.point:
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "target_moved")
        self._hit_test(target, snapshot.hwnd, moved_detail="target_moved")
        call.send("mouse.click", lambda: inputs.click_at(*target.point))
        self._last_click = (time.monotonic(), target.point)
        hit = inputs.runtime_id_at(*target.point)
        evidence["hit_after"] = "target" if hit in target.subtree else "other"
        self._debug.capture("aim", lambda: _aim_record(snapshot, target, hit))
        self._debug.capture("neighbourhood", lambda: _neighbourhood(snapshot, target.element))
        if action.postcondition is None:
            return _unverified(call, {"reason": "no_postcondition", **evidence})
        return _finish(call, _poll_postcondition(action.postcondition), evidence)

    def _wait_out_double_click(self, point: tuple[int, int]) -> None:
        if self._last_click is None:
            return
        stamp, last_point = self._last_click
        size = self._inputs.double_click_size_px()
        near = abs(point[0] - last_point[0]) <= size and abs(point[1] - last_point[1]) <= size
        remaining = self._inputs.double_click_time_s() - (time.monotonic() - stamp)
        if near and remaining > 0:
            time.sleep(remaining)

    # ---- press -------------------------------------------------------------------------------

    def _press(
        self, action: PhysicalAction, snapshot: Snapshot, target: _Target, call: Call
    ) -> ActionResult:
        chord = action.chord
        if chord is None:  # enforced by PhysicalAction.__post_init__; kept for the type checker
            raise SteerError(ErrorCode.GATE_BLOCKED, "press needs a chord")
        if keyboard.is_routed_combo(chord):
            raise RefusalError(ErrorCode.GATE_BLOCKED, "routed_chord")
        if keyboard.is_blocked_combo(chord) and action.approval is None:
            raise RefusalError(ErrorCode.GATE_BLOCKED, "blocked_chord")
        evidence: dict[str, Any] = {"foreground": self._bring_forward(snapshot.hwnd)}
        self._refuse_menu_mode(snapshot.hwnd)
        if action.approval is not None:
            evidence["decision_id"] = action.approval.decision_id
        focused = self._inputs.focused_runtime_id()
        if not _focus_on_field(snapshot, target, focused):
            raise RefusalError(ErrorCode.FOCUS_LOST, _unfocused_detail(snapshot, focused))
        self._check_hands_off()
        try:
            call.send("keyboard.chord", lambda: self._inputs.press(chord, action.approval))
        except InjectionError as exc:
            return _partial(call, exc, evidence)
        if action.postcondition is None:
            return _unverified(call, {"reason": "no_postcondition", **evidence})
        return _finish(call, _poll_postcondition(action.postcondition), evidence)

    # ---- type --------------------------------------------------------------------------------

    def _type(
        self, action: PhysicalAction, snapshot: Snapshot, target: _Target, call: Call
    ) -> ActionResult:
        plan = self._type_plan(action, target)
        current = plan.read()
        if current == plan.wanted:
            return ActionResult.already_satisfied(action.verb)
        if bool(plan.pattern.CurrentIsReadOnly):
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "read_only")
        if current != "":
            raise RefusalError(ErrorCode.GATE_PENDING_USER, "field_not_empty")
        evidence: dict[str, Any] = {"foreground": self._bring_forward(snapshot.hwnd)}
        self._refuse_menu_mode(snapshot.hwnd)
        if action.approval is not None:
            evidence["decision_id"] = action.approval.decision_id
        if self._inputs.focused_runtime_id() not in target.subtree:
            raise RefusalError(ErrorCode.FOCUS_LOST, "target_not_focused")
        outcome = self._type_send(snapshot.hwnd, target, call, plan.bursts, evidence)
        if isinstance(outcome, ActionResult):
            return outcome
        evidence = _with_count(evidence, call, "chunks", outcome)
        if not call.secure:
            evidence["chars"] = plan.units
        return self._type_readback(call, plan, evidence)

    def _type_plan(self, action: PhysicalAction, target: _Target) -> _TypePlan:
        """Validate the text and the target's readability; nothing is sent here."""
        if action.text is None:  # enforced by PhysicalAction.__post_init__; for the type checker
            raise SteerError(ErrorCode.INVALID_TEXT, "type needs text")
        if action.approval is not None and not target.element.secure:
            raise RefusalError(ErrorCode.GATE_BLOCKED, "approval_mismatch")
        parts = sanitize_text(action.text, newline=action.newline)
        if _TAB in action.text:
            raise RefusalError(ErrorCode.INVALID_TEXT, "tab_not_typed")
        wanted = normalise_newlines(action.text)
        units = _units(wanted)
        if units > self._pace.max_units_per_step:
            raise RefusalError(ErrorCode.BUDGET_EXCEEDED, "typing_budget")
        if not target.handle.GetCurrentPattern(UIA_VALUE_PATTERN):
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "unreadable_value")
        pattern = self._uia.pattern(target.handle, UIA_VALUE_PATTERN, "ValuePattern")

        role = target.element.role

        def read() -> str:
            value = pattern.CurrentValue
            return "" if value is None else field_text(str(value), role)

        return _TypePlan(
            pattern=pattern, read=read, wanted=wanted, units=units, bursts=_bursts(parts)
        )

    def _type_send(
        self,
        hwnd: int,
        target: _Target,
        call: Call,
        bursts: list[_Burst],
        evidence: dict[str, Any],
    ) -> ActionResult | int:
        """Send the bursts; the count sent, or the result that stopped the typing early."""
        sent = 0
        call.sent = "keyboard.type"
        for index, burst in enumerate(bursts):
            if (token := self._kill_token()) is not None:
                return self._stopped(call, token, sent, evidence, killed=True)
            if (stop := self._burst_blocker(hwnd, target.subtree)) is not None:
                return self._stopped(call, stop, sent, evidence)
            try:
                if burst.kind == "text":
                    self._inputs.type_text(burst.payload)
                else:
                    self._inputs.press(burst.payload)
            except InjectionError as exc:
                return _partial(call, exc, _with_count(evidence, call, "chunks", sent))
            sent += 1
            if self._pace.chunk_delay_s and index < len(bursts) - 1:
                time.sleep(self._pace.chunk_delay_s)
        if (stop := self._burst_blocker(hwnd, target.subtree)) is not None:
            return self._stopped(call, stop, sent, evidence)
        call.steps.append(
            Step(label="keyboard.type", mechanism=call.mechanism, outcome="succeeded")
        )
        return sent

    def _type_readback(self, call: Call, plan: _TypePlan, evidence: dict[str, Any]) -> ActionResult:
        readback = poll_state(plan.read, plan.wanted)
        if not readback.verified and readback.settled:
            final = plan.read()
            if plan.wanted.startswith(final):
                evidence["detail"] = "truncated"
                if not call.secure:
                    evidence["units_read"] = _units(final)
            else:
                evidence["detail"] = "readback_differs"
        return _finish(call, readback, evidence)

    def _burst_blocker(self, hwnd: int, subtree: frozenset[tuple[int, ...]]) -> str | None:
        """Why the next burst must not go out, as a fixed token, or None."""
        inputs = self._inputs
        if inputs.foreground_hwnd() != hwnd:
            return "focus_lost"
        if inputs.focused_runtime_id() not in subtree:
            return "focus_moved"
        if inputs.physical_modifiers_down():
            return "modifier_held"
        if inputs.physical_buttons_down():
            return "button_held"
        return None

    def _kill_token(self) -> str | None:
        return None if self._blocker is None else self._blocker()

    def _stopped(
        self,
        call: Call,
        reason: str,
        sent: int,
        evidence: dict[str, Any],
        *,
        killed: bool = False,
    ) -> ActionResult:
        """Typing stopped between bursts: nothing sent is a refusal, else delivery is
        uncertain; a kill carries `gate_blocked`, a lost focus `focus_lost` (TK-06)."""
        if sent == 0:
            call.sent = None
            raise RefusalError(ErrorCode.GATE_BLOCKED if killed else ErrorCode.FOCUS_LOST, reason)
        extra = _with_count({}, call, "chunks", sent)
        if reason == "focus_lost":
            extra["foreground_process"] = self._inputs.foreground_process(
                self._inputs.foreground_hwnd()
            )
        details = {"reason": reason, "phase": "call", **evidence, **extra}
        if killed:
            details["detail"] = reason
        result = _uncertain(call, details)
        return replace(result, code=ErrorCode.GATE_BLOCKED) if killed else result


_DISPATCH: dict[
    str, Callable[[PhysicalExecutor, PhysicalAction, Snapshot, _Target, Call], ActionResult]
] = {
    "click": PhysicalExecutor._click,
    "press": PhysicalExecutor._press,
    "type": PhysicalExecutor._type,
}


# ---- helpers -------------------------------------------------------------------------------------


def _rect(handle: Any) -> Box:
    rect = handle.CurrentBoundingRectangle
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def _aim(live: Box, visible: Box, row: int | None) -> tuple[int, int] | None:
    """The click point: the visible part's centre, or for a tree item with an own row
    ([v2.81]) the middle of that row on the live rectangle, horizontally the visible centre;
    None when that row lies outside the visible part (TE-08)."""
    if row is None:
        return centre_of(visible)
    point = ((visible[0] + visible[2]) // 2, live[1] + row // 2)
    return point if visible[1] <= point[1] < visible[3] else None


def _within(cursor: tuple[int, int], point: tuple[int, int], tolerance: int, box: Box) -> bool:
    near = abs(cursor[0] - point[0]) <= tolerance and abs(cursor[1] - point[1]) <= tolerance
    inside = box[0] <= cursor[0] < box[2] and box[1] <= cursor[1] < box[3]
    return near and inside


def _visible_part(live: Box, window: Box, screen: Box) -> Box | None:
    """The part of the live rectangle inside both the window and the virtual screen (PX-20): a
    window taller than the screen leaves its bottom controls unreachable by the cursor."""
    in_window = _intersect(live, window)
    return None if in_window is None else _intersect(in_window, screen)


def _intersect(a: Box, b: Box) -> Box | None:
    box = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return box if box[2] > box[0] and box[3] > box[1] else None


def _focus_on_field(snapshot: Snapshot, target: _Target, focused: tuple[int, ...]) -> bool:
    """Keyboard focus sits on the target itself or on a text-role descendant of it; a link or a
    button inside a page's Document is never the field a chord was approved for (SE-01)."""
    if not focused:
        return False
    if focused == tuple(target.element.runtime_id):
        return True
    if focused not in target.subtree:
        return False
    return any(tuple(e.runtime_id) == focused and e.role in TEXT_ROLES for e in snapshot.elements)


def _unfocused_detail(snapshot: Snapshot, focused: tuple[int, ...]) -> str:
    """Why a keyboard verb's field is not focused, as a fixed token: `active_option` when the
    UIA focus, by RuntimeId in the step's snapshot, is a popup option (D-151), else
    `target_not_focused`."""
    if focused and any(
        tuple(e.runtime_id) == focused and e.role in POPUP_OPTION_ROLES for e in snapshot.elements
    ):
        return "active_option"
    return "target_not_focused"


def _subtree_ids(snapshot: Snapshot, element: Element) -> frozenset[tuple[int, ...]]:
    children: dict[int | None, list[Element]] = {}
    for candidate in snapshot.elements:
        children.setdefault(candidate.parent_id, []).append(candidate)
    ids: set[tuple[int, ...]] = set()
    stack = [element]
    while stack:
        node = stack.pop()
        if node.runtime_id:
            ids.add(node.runtime_id)
        stack.extend(children.get(node.id, []))
    return frozenset(ids)


def _ancestor_ids(snapshot: Snapshot, element: Element) -> frozenset[tuple[int, ...]]:
    by_id = {candidate.id: candidate for candidate in snapshot.elements}
    ids: set[tuple[int, ...]] = set()
    parent_id = element.parent_id
    while parent_id is not None and parent_id in by_id:
        parent = by_id[parent_id]
        if parent.runtime_id:
            ids.add(parent.runtime_id)
        parent_id = parent.parent_id
    return frozenset(ids)


def _bursts(parts: list[tuple[str, str]]) -> list[_Burst]:
    bursts: list[_Burst] = []
    for kind, value in parts:
        if kind == "text":
            bursts.extend(_Burst("text", chunk) for chunk in keyboard.chunk_text(value))
        else:
            bursts.append(_Burst("chord", value))
    return bursts


def _with_count(evidence: dict[str, Any], call: Call, key: str, value: int) -> dict[str, Any]:
    return evidence if call.secure else {**evidence, key: value}


def _poll_postcondition(postcondition: Callable[[], bool]) -> Readback:
    return Readback(verified=poll_until(postcondition), settled=False)


def _finish(call: Call, readback: Readback, evidence: dict[str, Any]) -> ActionResult:
    return ActionResult(
        verb=call.verb,
        delivery=readback.delivery,
        steps=call.steps,
        code=readback.code,
        evidence=evidence,
    )


def _unverified(call: Call, evidence: dict[str, Any]) -> ActionResult:
    return ActionResult(
        verb=call.verb,
        delivery=Delivery.DELIVERED_UNVERIFIED,
        steps=call.steps,
        code=ErrorCode.VERIFY_TIMEOUT,
        evidence=evidence,
    )


def _uncertain(call: Call, evidence: dict[str, Any]) -> ActionResult:
    if call.sent is not None and not call.call_returned:
        call.steps.append(Step(label=call.sent, mechanism=call.mechanism, outcome="failed"))
    return ActionResult(
        verb=call.verb,
        delivery=Delivery.DELIVERY_UNCERTAIN,
        steps=call.steps,
        code=ErrorCode.VERIFY_FAILED,
        evidence=evidence,
    )


def _partial(call: Call, exc: InjectionError, evidence: dict[str, Any]) -> ActionResult:
    counts = {} if call.secure else {"inserted": exc.inserted, "sent": exc.sent}
    return _uncertain(call, {"reason": "partial_injection", "phase": "call", **evidence, **counts})


def tap_refusal(inputs: InputPort) -> str | None:
    """Why the loop must not bring a window forward now, from the executor's own readers
    (issue #38; hunter SF-01/05/06/20): a secure desktop; a key or button the person holds; an
    accessibility filter (Sticky Keys would latch the tap); a foreground window running above
    this process's integrity, whose typing the takeover hook cannot see. None: tap."""
    if inputs.input_desktop_name() != "Default":
        return "secure_desktop"
    if inputs.physical_modifiers_down() or inputs.physical_buttons_down():
        return "hands_on"
    if inputs.accessibility_filters_active():
        return "accessibility_filter"
    front = inputs.foreground_hwnd()
    if front:
        allowed = integrity_allows_input(
            caller_level=inputs.caller_integrity_level(),
            target_level=inputs.integrity_level(inputs.window_pid(front)),
        )
        if not allowed:
            return "foreground_elevated"
    return None
