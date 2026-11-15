"""The guards the act port puts around v1's executors (adapter slice 6, packet rev 3.9).

v1's executors are reused unchanged; these wrappers are what slice 7 injects into them:
- `InputGuard` wraps v1's `InputPort` (the physical executor's Win32 side). Every call that sends
  input (a cursor move, a click, each typed burst, each chord, a foreground acquire with its key
  nudge) reads the switch first and is refused once it reports (AP-01). Keyboard input goes only
  while the act port has armed it, only to the exact RuntimeIds armed (never "any descendant",
  AP-10), and only when the focused element's live IsPassword reads False (AP-08). The
  double-click wait is the guard's own, kill-aware (AP-02).
- `ToggleGuard` wraps v1's UIA bindings so the TogglePattern v1 gets sends a second Toggle in one
  actuation only when the state re-read just before it still shows the third state v1 saw
  ([V2.52] (a), which corrects [V2.51] (4)).
- `GuardDesk` is what the act port reads before physical input: v1's tap refusal and the live
  keyboard focus, both fail closed.

A refusal is v1's `RefusalError`, so v1's own `classify` records it by phase: nothing sent is
`not_delivered`, a refusal after a burst went out is `delivery_uncertain`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Final

from steerai.action.execution import UIA_TOGGLE_PATTERN, RefusalError, UiaBindings
from steerai.action.keyboard import GateApproval
from steerai.action.physical import InputPort, tap_refusal
from steerai.errors import ErrorCode, SteerError
from steerai.loop2.decision import Key
from steerai.loop2.loop import PortInterrupted
from steerai.loop2.world_ports import kill_sleep
from steerai.world.elements import Box

KEY_CHORDS: Final[Mapping[Key, str]] = {
    "Enter": "enter",
    "Escape": "escape",
    "Tab": "tab",
    "Up": "up",
    "Down": "down",
    "Left": "left",
    "Right": "right",
    "PageUp": "pageup",
    "PageDown": "pagedown",
    "Home": "home",
    "End": "end",
    "Space": "space",
    "Backspace": "backspace",
}
"""AP-12: each `Key` to one of v1's named keys, no modifier; a test locks it to the Literal."""
TOGGLE_STATES: Final = frozenset({0, 1, 2})
"""UIA ToggleState (UIAutomationCore.h): Off 0, On 1, Indeterminate 2."""
TOGGLE_LIMIT: Final = 2
"""[V2.52] (a): a tri-state control reaches any state from any other in at most two Toggles."""
SECOND_REFUSED: Final = "second_toggle_refused"
SETTLED: Final = "settled_on_reread"


class InputGuard:
    """v1's `InputPort`, with the switch read before every input-sending call (AP-01)."""

    def __init__(
        self,
        inner: InputPort,
        *,
        interrupted: Callable[[], str | None],
        focus_secure: Callable[[], bool | None],
        clock: Callable[[], float],
        pause: Callable[[float], None] = time.sleep,
    ) -> None:
        self._inner = inner
        self._interrupted = interrupted
        self._focus_secure = focus_secure
        """The focused element's live IsPassword: True a password, None unread (slice 7)."""
        self._clock = clock
        self._pause = pause
        self._armed: frozenset[tuple[int, ...]] | None = None
        self._last_click: float | None = None

    # ---- the act port's arming -----------------------------------------------------------------

    def armed(self, focus: frozenset[tuple[int, ...]]) -> AbstractContextManager[None]:
        """Keyboard input is allowed only inside this block, to these RuntimeIds."""
        return self._arm(focus)

    @contextmanager
    def _arm(self, focus: frozenset[tuple[int, ...]]) -> Iterator[None]:
        self._armed = focus
        try:
            yield
        finally:
            self._armed = None

    # ---- the checks ------------------------------------------------------------------------------

    def _switch(self) -> None:
        token = self._interrupted()
        if token is not None:
            raise RefusalError(ErrorCode.GATE_BLOCKED, token)

    def _keyboard(self) -> None:
        self._switch()
        if self._armed is None:
            raise RefusalError(ErrorCode.GATE_BLOCKED, "not_armed")
        if self._inner.focused_runtime_id() not in self._armed:
            raise RefusalError(ErrorCode.FOCUS_LOST, "focus_moved")
        secure = self._focus_secure()
        if secure is None:
            raise RefusalError(ErrorCode.GATE_BLOCKED, "secure_unread")
        if secure:
            raise RefusalError(ErrorCode.GATE_BLOCKED, "secure_field")

    # ---- the sending calls -----------------------------------------------------------------------

    def acquire_foreground(self, hwnd: int) -> str:
        self._switch()
        return self._inner.acquire_foreground(hwnd)

    def move_cursor(self, x: int, y: int) -> None:
        self._switch()
        self._inner.move_cursor(x, y)

    def click_at(self, x: int, y: int) -> None:
        self._switch()
        self._inner.click_at(x, y)
        self._last_click = self._clock()

    def type_text(self, text: str) -> None:
        self._keyboard()
        self._inner.type_text(text)

    def press(self, chord: str, approval: GateApproval | None = None) -> None:
        self._keyboard()
        self._inner.press(chord, approval)

    def double_click_time_s(self) -> float:
        """AP-02: v1 sleeps out the double-click time before a click near the last one, with
        `time.sleep`. The guard waits it out first, from its own last click, in kill-aware steps
        (whatever the distance: never less safe than v1), and answers 0.0, so v1's own remainder
        is never positive and it never sleeps. v1 reads this only for that wait."""
        if self._last_click is not None:
            left = self._inner.double_click_time_s() - (self._clock() - self._last_click)
            try:
                kill_sleep(left, interrupted=self._interrupted, pause=self._pause)
            except PortInterrupted as stop:
                raise RefusalError(ErrorCode.GATE_BLOCKED, stop.token) from stop
        return 0.0

    # ---- the reads, passed through -------------------------------------------------------------

    def dpi_aware(self) -> bool:
        return self._inner.dpi_aware()

    def input_desktop_name(self) -> str:
        return self._inner.input_desktop_name()

    def accessibility_filters_active(self) -> bool:
        return self._inner.accessibility_filters_active()

    def own_pid(self) -> int:
        return self._inner.own_pid()

    def is_hung(self, hwnd: int) -> bool:
        return self._inner.is_hung(hwnd)

    def integrity_level(self, pid: int) -> int | None:
        return self._inner.integrity_level(pid)

    def caller_integrity_level(self) -> int:
        return self._inner.caller_integrity_level()

    def window_rect(self, hwnd: int) -> Box:
        return self._inner.window_rect(hwnd)

    def screen_rect(self) -> Box:
        return self._inner.screen_rect()

    def root_hwnd_at(self, x: int, y: int) -> int:
        return self._inner.root_hwnd_at(x, y)

    def runtime_id_at(self, x: int, y: int) -> tuple[int, ...]:
        return self._inner.runtime_id_at(x, y)

    def physical_modifiers_down(self) -> list[str]:
        return self._inner.physical_modifiers_down()

    def physical_buttons_down(self) -> list[str]:
        return self._inner.physical_buttons_down()

    def foreground_hwnd(self) -> int:
        return self._inner.foreground_hwnd()

    def foreground_process(self, hwnd: int) -> str:
        return self._inner.foreground_process(hwnd)

    def window_pid(self, hwnd: int) -> int:
        return self._inner.window_pid(hwnd)

    def in_menu_mode(self, hwnd: int) -> bool:
        return self._inner.in_menu_mode(hwnd)

    def focused_runtime_id(self) -> tuple[int, ...]:
        return self._inner.focused_runtime_id()

    def double_click_size_px(self) -> int:
        return self._inner.double_click_size_px()

    def cursor_pos(self) -> tuple[int, int]:
        return self._inner.cursor_pos()


class GuardDesk:
    """`act_port.Desk` over the guard: what the act port reads before physical input."""

    def __init__(self, guard: InputGuard, *, last_input: Callable[[], float]) -> None:
        self._guard = guard
        self._last_input = last_input
        """The person's last input, on the act port's clock (slice 7: the session monitor)."""

    def tap_refusal(self) -> str | None:
        """v1's `tap_refusal`; a check that cannot be read refuses (`tap_unreadable`)."""
        try:
            return tap_refusal(self._guard)
        except (OSError, SteerError):
            return "tap_unreadable"

    def last_input(self) -> float:
        return self._last_input()

    def focused(self) -> tuple[int, ...] | None:
        """The live keyboard focus's RuntimeId; None when unread or empty."""
        try:
            focus = self._guard.focused_runtime_id()
        except (OSError, SteerError):
            return None
        return focus or None

    def armed(self, focus: frozenset[tuple[int, ...]]) -> AbstractContextManager[None]:
        return self._guard.armed(focus)


class ToggleGuard(UiaBindings):
    """v1's UIA bindings with the TogglePattern wrapped ([V2.52] (a))."""

    __slots__ = ()

    def pattern(self, handle: Any, pattern_id: int, name: str) -> Any:  # Any: COM objects
        raw = super().pattern(handle, pattern_id, name)
        return _OneSubstitution(raw) if pattern_id == UIA_TOGGLE_PATTERN else raw


class _OneSubstitution:
    """One actuation's TogglePattern, [V2.52] (a): v1's rule plus a fresh read. v1 sends a second
    Toggle only when its poll after the first settled on a third state (neither the initial one
    nor the wanted one). Just before that second Toggle the state is read again:
    - still that third state: the second Toggle goes (two is the hard limit);
    - the other tri-state value (the wanted one, arrived late): no second Toggle, a success
      (`settled_on_reread`; a second would move it off);
    - the initial state again, any other value, or unread: no second Toggle
      (`second_toggle_refused`).
    v1 reads `CurrentToggleState` and calls `Toggle` only."""

    def __init__(self, raw: Any) -> None:  # Any: a COM pattern object
        self._raw = raw
        self._toggles = 0
        self._initial: int | None = None
        self._polled: int | None = None

    @property
    def CurrentToggleState(self) -> Any:  # noqa: N802 - COM naming; Any: the COM value
        value = self._raw.CurrentToggleState
        if self._toggles == 0 and self._initial is None:
            self._initial = int(value)
        elif self._toggles == 1:
            self._polled = int(value)
        return value

    def Toggle(self) -> None:  # noqa: N802 - COM naming
        if self._toggles == 1:
            self._second()
        elif self._toggles >= TOGGLE_LIMIT:
            raise RefusalError(ErrorCode.VERIFY_FAILED, "toggle_limit")
        self._raw.Toggle()
        self._toggles += 1

    def _second(self) -> None:
        """Raise unless the fresh read still shows the third state v1's poll settled on."""
        try:
            now = int(self._raw.CurrentToggleState)
        except (OSError, SteerError, ValueError, TypeError) as exc:  # unread: never a second
            raise RefusalError(ErrorCode.VERIFY_FAILED, SECOND_REFUSED) from exc
        third, initial = self._polled, self._initial
        if third is not None and now == third and now != initial:
            return
        if third is not None and now in TOGGLE_STATES - {third, initial}:
            raise RefusalError(ErrorCode.VERIFY_FAILED, SETTLED)
        raise RefusalError(ErrorCode.VERIFY_FAILED, SECOND_REFUSED)
