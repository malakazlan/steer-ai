"""The semantic executor (design 5.9 [v1.1]): verbs through UIA patterns on the snapshot's live
handles.

Before any pattern call the executor admits the action: the snapshot must be stable and younger
than `SNAPSHOT_MAX_AGE_S`, the element enabled, a secure field covered by a typed `GateApproval`,
the window alive, uncloaked and enabled, and the handle's live control type and name equal to the
snapshot element (RuntimeIds are reusable, so identity is re-read, 5.7). A caller postcondition
that already holds refuses the action: a trivially true predicate can never read as verified.

Failures are classified by phase. Nothing sent yet: `not_delivered` with the error's taxonomy
code (COM HRESULTs map through `_HRESULT_CODES`, anything else is `perception_timeout`). From the
mutating call onward the action may have landed, so every error, including a programming error in
a postcondition, is `delivery_uncertain` with `verify_failed`; a programming error before the call
propagates because nothing happened and it is a bug.

Every verb reads its own state back (`poll_state`): equal before the call means already in state
and nothing is sent; settled on a different state is `verify_failed`; never settled is
`verify_timeout`. `set_value` replaces the value, never appends; newlines are literal content.
`check`/`uncheck` toggle at most `MAX_TOGGLES` times because three-state controls cycle. Physical
input (click at a point, keystrokes, chords) lives in the physical executor, not here.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from steerai.action.dispositions import ActionResult, Delivery, Step
from steerai.action.keyboard import GateApproval
from steerai.action.readback import Readback, poll_state, poll_until
from steerai.action.sanitize import normalise_newlines, sanitize_value
from steerai.errors import ErrorCode, SteerError
from steerai.probes import windows as win
from steerai.probes.uia_snapshot import control_type_names, load_uia_module
from steerai.world.elements import Element
from steerai.world.model import Snapshot, normalise_name

VERBS: frozenset[str] = frozenset(
    {"click", "set_value", "check", "uncheck", "expand", "collapse", "select", "scroll_into_view"}
)
_TEXT_VERBS = frozenset({"set_value"})
_POSTCONDITION_VERBS = frozenset({"click", "scroll_into_view"})
SNAPSHOT_MAX_AGE_S = 10.0
MAX_TOGGLES = 2

# UIA pattern ids (UIAutomationClient constants), kept as literals so the executor is testable
# without comtypes; the generated module exposes the same values.
UIA_INVOKE_PATTERN = 10000
UIA_VALUE_PATTERN = 10002
UIA_EXPAND_COLLAPSE_PATTERN = 10005
UIA_SELECTION_ITEM_PATTERN = 10010
UIA_TOGGLE_PATTERN = 10015
UIA_SCROLL_ITEM_PATTERN = 10017
_PATTERN_INTERFACES = {
    UIA_INVOKE_PATTERN: "IUIAutomationInvokePattern",
    UIA_VALUE_PATTERN: "IUIAutomationValuePattern",
    UIA_EXPAND_COLLAPSE_PATTERN: "IUIAutomationExpandCollapsePattern",
    UIA_SELECTION_ITEM_PATTERN: "IUIAutomationSelectionItemPattern",
    UIA_TOGGLE_PATTERN: "IUIAutomationTogglePattern",
    UIA_SCROLL_ITEM_PATTERN: "IUIAutomationScrollItemPattern",
}
_TOGGLE_OFF, _TOGGLE_ON = 0, 1
_COLLAPSED, _EXPANDED, _LEAF_NODE = 0, 1, 3

# HRESULTs from winerror.h and uiautomationcoreapi.h; anything else is perception_timeout.
E_ACCESSDENIED = 0x80070005
UIA_E_ELEMENTNOTENABLED = 0x80040200
UIA_E_ELEMENTNOTAVAILABLE = 0x80040201
RPC_S_SERVER_UNAVAILABLE = 0x800706BA
_HRESULT_CODES = {
    E_ACCESSDENIED: ErrorCode.ELEMENT_NOT_FOUND,
    UIA_E_ELEMENTNOTAVAILABLE: ErrorCode.ELEMENT_NOT_FOUND,
    UIA_E_ELEMENTNOTENABLED: ErrorCode.ELEMENT_NOT_ACTIONABLE,
    RPC_S_SERVER_UNAVAILABLE: ErrorCode.APP_CRASHED,
}


class WindowState(Protocol):
    def is_window(self, hwnd: int) -> bool: ...
    def is_cloaked(self, hwnd: int) -> bool: ...
    def is_window_enabled(self, hwnd: int) -> bool: ...


class LiveWindows:
    """The Win32 window re-check (SX-12) through the probes module."""

    def is_window(self, hwnd: int) -> bool:
        return win.is_window(hwnd)

    def is_cloaked(self, hwnd: int) -> bool:
        return win.is_cloaked(hwnd)

    def is_window_enabled(self, hwnd: int) -> bool:
        return win.is_window_enabled(hwnd)


@dataclass(frozen=True, slots=True)
class UiaBindings:
    """What the executor needs from the generated UIA module: role names and pattern interfaces.

    Resolved once at construction so a typelib missing an interface is a configuration error,
    not a per-action crash (SX-33). COM classes have no stubs, hence Any.
    """

    control_types: dict[int, str]
    interfaces: dict[int, Any]

    @classmethod
    def from_module(cls, module: Any) -> UiaBindings:
        interfaces: dict[int, Any] = {}
        for pattern_id, name in _PATTERN_INTERFACES.items():
            interface = getattr(module, name, None)
            if interface is None:
                raise OSError(f"UIAutomationClient typelib lacks {name}")
            interfaces[pattern_id] = interface
        return cls(control_types=control_type_names(module), interfaces=interfaces)

    @classmethod
    def load(cls) -> UiaBindings:
        if sys.platform != "win32":
            return cls(control_types={}, interfaces={})
        return cls.from_module(load_uia_module())


@dataclass(frozen=True, slots=True)
class Action:
    verb: str
    element_id: int
    text: str | None = field(default=None, repr=False)
    approval: GateApproval | None = None
    """The gate's typed approval for credential entry into a secure field (5.13)."""
    postcondition: Callable[[], bool] | None = None
    """Caller-supplied readback for `click` and `scroll_into_view`."""

    def __post_init__(self) -> None:
        if self.verb not in VERBS:
            raise ValueError(f"unknown verb {self.verb!r}")
        if self.verb in _TEXT_VERBS and self.text is None:
            raise ValueError(f"{self.verb} needs text")
        if self.verb not in _TEXT_VERBS and self.text is not None:
            raise ValueError(f"{self.verb} takes no text")
        if self.approval is not None and not isinstance(self.approval, GateApproval):
            raise TypeError("approval must be a GateApproval from the gate, not a flag")
        if self.postcondition is not None and self.verb not in _POSTCONDITION_VERBS:
            raise ValueError(f"{self.verb} reads its own state back; it takes no postcondition")


class _RefusalError(SteerError):
    """An executor refusal; `detail` is a fixed token safe to record as evidence (SX-28)."""

    def __init__(self, code: ErrorCode, detail: str) -> None:
        super().__init__(code, detail)
        self.detail = detail


@dataclass
class _Call:
    """One execution's step log and whether its mutating call has been issued."""

    verb: str
    secure: bool = False
    steps: list[Step] = field(default_factory=list)
    sent: str | None = None

    def send(self, label: str, mutate: Callable[[], Any]) -> None:
        self.sent = label
        mutate()
        self.steps.append(Step(label=label, mechanism="semantic", outcome="succeeded"))

    @property
    def call_returned(self) -> bool:
        return bool(self.steps) and self.steps[-1].label == self.sent


class Executor:
    def __init__(
        self,
        *,
        uia: UiaBindings | None = None,
        windows: WindowState | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._uia = UiaBindings.load() if uia is None else uia
        self._windows: WindowState = LiveWindows() if windows is None else windows
        self._clock = clock
        """Same clock as `Snapshot.taken_at` (wall time) for the age check."""

    def execute(self, action: Action, snapshot: Snapshot) -> ActionResult:
        call = _Call(action.verb)
        try:
            element, handle = self._admit(action, snapshot)
            call.secure = element.secure
            return _DISPATCH[action.verb](self, action, handle, call)
        except Exception as exc:
            result = _classify(call, exc)
            if result is None:
                raise
            return result

    # ---- admission (design 5.9 [v1.1] pre-call checks) -----------------------------------------

    def _admit(self, action: Action, snapshot: Snapshot) -> tuple[Element, Any]:
        element = snapshot.element(action.element_id)
        handle = snapshot.handle(action.element_id)
        if not snapshot.stable:
            raise _RefusalError(ErrorCode.UNSTABLE_UI, "snapshot_unstable")
        if self._clock() - snapshot.taken_at > SNAPSHOT_MAX_AGE_S:
            raise _RefusalError(ErrorCode.UNSTABLE_UI, "snapshot_stale")
        if not element.enabled:
            raise _RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "disabled")
        if element.secure and action.approval is None:
            raise _RefusalError(ErrorCode.GATE_BLOCKED, "secure_field")
        if not self._windows.is_window(snapshot.hwnd):
            raise _RefusalError(ErrorCode.APP_CRASHED, "window_gone")
        if self._windows.is_cloaked(snapshot.hwnd):
            raise _RefusalError(ErrorCode.WINDOW_OTHER_DESKTOP, "window_cloaked")
        if not self._windows.is_window_enabled(snapshot.hwnd):
            raise _RefusalError(ErrorCode.INTERRUPT_MODAL, "window_disabled")
        type_id = int(handle.CurrentControlType)
        role = self._uia.control_types.get(type_id, str(type_id))
        name = normalise_name(str(handle.CurrentName or ""))
        if role != element.role or name != normalise_name(element.name):
            raise _RefusalError(ErrorCode.ELEMENT_NOT_FOUND, "identity_changed")
        if action.postcondition is not None and action.postcondition():
            raise _RefusalError(ErrorCode.VERIFY_FAILED, "precondition_already_satisfied")
        return element, handle

    def _pattern(self, handle: Any, pattern_id: int, name: str) -> Any:
        # COM pattern objects have no stubs; the executor reads only the documented members.
        raw = handle.GetCurrentPattern(pattern_id)
        if not raw:
            raise _RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"no_{name}")
        interface = self._uia.interfaces.get(pattern_id)
        query = getattr(raw, "QueryInterface", None)
        return raw if query is None or interface is None else query(interface)

    # ---- verbs -------------------------------------------------------------------------------

    def _click(self, action: Action, handle: Any, call: _Call) -> ActionResult:
        pattern = self._pattern(handle, UIA_INVOKE_PATTERN, "InvokePattern")
        call.send("InvokePattern.Invoke", pattern.Invoke)
        if action.postcondition is None:
            return _unverified(call, "no_postcondition")
        return _finish(call, _poll_postcondition(action.postcondition))

    def _set_value(self, action: Action, handle: Any, call: _Call) -> ActionResult:
        if action.text is None:  # enforced by Action.__post_init__; kept for the type checker
            raise SteerError(ErrorCode.INVALID_TEXT, "set_value needs text")
        wanted = sanitize_value(action.text)
        pattern = self._pattern(handle, UIA_VALUE_PATTERN, "ValuePattern")

        def read() -> str:
            raw = pattern.CurrentValue
            return "" if raw is None else normalise_newlines(str(raw))

        if read() == wanted:
            return ActionResult.already_satisfied(action.verb)
        if bool(pattern.CurrentIsReadOnly):
            raise _RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "read_only")
        call.send("ValuePattern.SetValue", lambda: pattern.SetValue(wanted))
        evidence: dict[str, Any] = {}
        if action.approval is not None:
            evidence["decision_id"] = action.approval.decision_id
        if not call.secure:
            evidence["chars"] = len(wanted)
        return _finish(call, poll_state(read, wanted), evidence)

    def _toggle_to(self, action: Action, handle: Any, call: _Call, wanted: int) -> ActionResult:
        pattern = self._pattern(handle, UIA_TOGGLE_PATTERN, "TogglePattern")

        def read() -> int:
            return int(pattern.CurrentToggleState)

        if read() == wanted:
            return ActionResult.already_satisfied(action.verb)
        toggles = 0
        while True:
            call.send("TogglePattern.Toggle", pattern.Toggle)
            toggles += 1
            readback = poll_state(read, wanted)
            if readback.verified or not readback.settled or toggles >= MAX_TOGGLES:
                return _finish(call, readback, {"toggles": toggles})

    def _check(self, action: Action, handle: Any, call: _Call) -> ActionResult:
        return self._toggle_to(action, handle, call, _TOGGLE_ON)

    def _uncheck(self, action: Action, handle: Any, call: _Call) -> ActionResult:
        return self._toggle_to(action, handle, call, _TOGGLE_OFF)

    def _expand_to(self, action: Action, handle: Any, call: _Call, wanted: int) -> ActionResult:
        pattern = self._pattern(handle, UIA_EXPAND_COLLAPSE_PATTERN, "ExpandCollapsePattern")

        def read() -> int:
            return int(pattern.CurrentExpandCollapseState)

        state = read()
        if state == wanted:
            return ActionResult.already_satisfied(action.verb)
        if state == _LEAF_NODE:
            raise _RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "leaf_node")
        if wanted == _EXPANDED:
            call.send("ExpandCollapsePattern.Expand", pattern.Expand)
        else:
            call.send("ExpandCollapsePattern.Collapse", pattern.Collapse)
        return _finish(call, poll_state(read, wanted))

    def _expand(self, action: Action, handle: Any, call: _Call) -> ActionResult:
        return self._expand_to(action, handle, call, _EXPANDED)

    def _collapse(self, action: Action, handle: Any, call: _Call) -> ActionResult:
        return self._expand_to(action, handle, call, _COLLAPSED)

    def _select(self, action: Action, handle: Any, call: _Call) -> ActionResult:
        pattern = self._pattern(handle, UIA_SELECTION_ITEM_PATTERN, "SelectionItemPattern")

        def read() -> bool:
            return bool(pattern.CurrentIsSelected)

        if read():
            return ActionResult.already_satisfied(action.verb)
        call.send("SelectionItemPattern.Select", pattern.Select)
        return _finish(call, poll_state(read, True))

    def _scroll_into_view(self, action: Action, handle: Any, call: _Call) -> ActionResult:
        pattern = self._pattern(handle, UIA_SCROLL_ITEM_PATTERN, "ScrollItemPattern")

        def on_screen() -> bool:
            return not bool(handle.CurrentIsOffscreen)

        if action.postcondition is None and on_screen():
            return ActionResult.already_satisfied(action.verb)
        call.send("ScrollItemPattern.ScrollIntoView", pattern.ScrollIntoView)
        if action.postcondition is None:
            return _finish(call, poll_state(on_screen, True))
        return _finish(call, _poll_postcondition(action.postcondition))


_DISPATCH: dict[str, Callable[[Executor, Action, Any, _Call], ActionResult]] = {
    "click": Executor._click,
    "set_value": Executor._set_value,
    "check": Executor._check,
    "uncheck": Executor._uncheck,
    "expand": Executor._expand,
    "collapse": Executor._collapse,
    "select": Executor._select,
    "scroll_into_view": Executor._scroll_into_view,
}


def _poll_postcondition(postcondition: Callable[[], bool]) -> Readback:
    """A caller predicate that stays false is not a definite wrong state: never `settled`."""
    return Readback(verified=poll_until(postcondition), settled=False)


def _finish(
    call: _Call, readback: Readback, evidence: dict[str, Any] | None = None
) -> ActionResult:
    return ActionResult(
        verb=call.verb,
        delivery=readback.delivery,
        steps=call.steps,
        code=readback.code,
        evidence=evidence or {},
    )


def _unverified(call: _Call, reason: str) -> ActionResult:
    return ActionResult(
        verb=call.verb,
        delivery=Delivery.DELIVERED_UNVERIFIED,
        steps=call.steps,
        code=ErrorCode.VERIFY_TIMEOUT,
        evidence={"reason": reason},
    )


def _code_for(exc: Exception) -> tuple[ErrorCode | None, dict[str, Any]]:
    """Taxonomy code and whitelisted evidence for an exception; None for a programming error."""
    if isinstance(exc, SteerError):
        extra = {"detail": exc.detail} if isinstance(exc, _RefusalError) else {}
        return exc.code, extra
    hresult = getattr(exc, "hresult", None)  # _ctypes.COMError; comtypes has no stubs
    if isinstance(hresult, int):
        unsigned = hresult & 0xFFFFFFFF
        code = _HRESULT_CODES.get(unsigned, ErrorCode.PERCEPTION_TIMEOUT)
        return code, {"hresult": f"0x{unsigned:08X}"}
    if isinstance(exc, OSError):
        return ErrorCode.PERCEPTION_TIMEOUT, {}
    return None, {}


def _classify(call: _Call, exc: Exception) -> ActionResult | None:
    """Phase-tracked failure classification; None means re-raise (bug before anything was sent)."""
    code, extra = _code_for(exc)
    if call.sent is None:
        if code is None:
            return None
        return ActionResult(
            verb=call.verb,
            delivery=Delivery.NOT_DELIVERED,
            steps=call.steps,
            code=code,
            evidence={"reason": code.value, **extra},
        )
    if call.call_returned:
        phase = "readback"
    else:
        phase = "call"
        call.steps.append(Step(label=call.sent, mechanism="semantic", outcome="failed"))
    evidence: dict[str, Any] = {"reason": (code or ErrorCode.VERIFY_FAILED).value, "phase": phase}
    if code is None:
        evidence["error"] = type(exc).__name__
    return ActionResult(
        verb=call.verb,
        delivery=Delivery.DELIVERY_UNCERTAIN,
        steps=call.steps,
        code=ErrorCode.VERIFY_FAILED,
        evidence={**evidence, **extra},
    )
