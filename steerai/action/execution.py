"""What every executor shares (design 5.9 [v1.1]): admission, the call log and failure
classification by phase.

Admission checks, before any pattern call or input: the snapshot is stable and younger than
`SNAPSHOT_MAX_AGE_S`, the element enabled, a secure field covered by a typed `GateApproval`, the
window alive, uncloaked and enabled, the handle's live control type and name equal to the snapshot
element (RuntimeIds are reusable, so identity is re-read, 5.7), and a caller postcondition does not
already hold (a trivially true predicate can never read as verified).

Classification: nothing sent yet → `not_delivered` with the error's taxonomy code (COM HRESULTs map
through `HRESULT_CODES`, other OS errors are `perception_timeout`, a programming error propagates
because nothing happened and it is a bug). From the mutating call onward the action may have
landed, so every error is `delivery_uncertain` with `verify_failed`.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from steerai.action.dispositions import ActionResult, Delivery, Mechanism, Step
from steerai.action.keyboard import GateApproval
from steerai.errors import ErrorCode, SteerError
from steerai.probes import windows as win
from steerai.probes.uia_snapshot import control_type_names, load_uia_module
from steerai.world.elements import Element
from steerai.world.model import Snapshot, normalise_name

SNAPSHOT_MAX_AGE_S = 10.0

# UIA pattern ids (UIAutomationClient constants), kept as literals so executors are testable
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

# HRESULTs from winerror.h and uiautomationcoreapi.h; anything else is perception_timeout.
E_ACCESSDENIED = 0x80070005
UIA_E_ELEMENTNOTENABLED = 0x80040200
UIA_E_ELEMENTNOTAVAILABLE = 0x80040201
RPC_S_SERVER_UNAVAILABLE = 0x800706BA
HRESULT_CODES = {
    E_ACCESSDENIED: ErrorCode.ELEMENT_NOT_FOUND,
    UIA_E_ELEMENTNOTAVAILABLE: ErrorCode.ELEMENT_NOT_FOUND,
    UIA_E_ELEMENTNOTENABLED: ErrorCode.ELEMENT_NOT_ACTIONABLE,
    RPC_S_SERVER_UNAVAILABLE: ErrorCode.APP_CRASHED,
}


class WindowState(Protocol):
    def is_window(self, hwnd: int) -> bool: ...
    def is_cloaked(self, hwnd: int) -> bool | None: ...
    def is_window_enabled(self, hwnd: int) -> bool: ...


class LiveWindows:
    """The Win32 window re-check (SX-12) through the probes module."""

    def is_window(self, hwnd: int) -> bool:
        return win.is_window(hwnd)

    def is_cloaked(self, hwnd: int) -> bool | None:
        return win.is_cloaked(hwnd)

    def is_window_enabled(self, hwnd: int) -> bool:
        return win.is_window_enabled(hwnd)


@dataclass(frozen=True, slots=True)
class UiaBindings:
    """What executors need from the generated UIA module: role names and pattern interfaces.

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

    def pattern(self, handle: Any, pattern_id: int, name: str) -> Any:
        """The handle's pattern object, or `element_not_actionable` when it exposes none."""
        # COM pattern objects have no stubs; executors read only the documented members.
        raw = handle.GetCurrentPattern(pattern_id)
        if not raw:
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"no_{name}")
        interface = self.interfaces.get(pattern_id)
        query = getattr(raw, "QueryInterface", None)
        return raw if query is None or interface is None else query(interface)


class Admissible(Protocol):
    """The part of an action that admission reads."""

    @property
    def element_id(self) -> int: ...
    @property
    def approval(self) -> GateApproval | None: ...
    @property
    def postcondition(self) -> Callable[[], bool] | None: ...


class RefusalError(SteerError):
    """An executor refusal; `detail` is a fixed token safe to record as evidence (SX-28)."""

    def __init__(self, code: ErrorCode, detail: str) -> None:
        super().__init__(code, detail)
        self.detail = detail


@dataclass
class Call:
    """One execution's step log and whether its mutating call has been issued."""

    verb: str
    mechanism: Mechanism
    secure: bool = False
    steps: list[Step] = field(default_factory=list)
    sent: str | None = None
    blocker: Callable[[], str | None] | None = None
    """Asked before every mutating call: a token (`killed`) refuses it, nothing is issued."""

    def send(self, label: str, mutate: Callable[[], Any]) -> None:
        if self.blocker is not None and (token := self.blocker()) is not None:
            raise RefusalError(ErrorCode.GATE_BLOCKED, token)
        self.sent = label
        mutate()
        self.steps.append(Step(label=label, mechanism=self.mechanism, outcome="succeeded"))

    @property
    def call_returned(self) -> bool:
        return bool(self.steps) and self.steps[-1].label == self.sent


@dataclass(frozen=True, slots=True)
class Admission:
    """Admission dependencies: the window re-check, the snapshot clock and the UIA bindings."""

    windows: WindowState
    bindings: UiaBindings
    clock: Callable[[], float] = time.time
    """Same clock as `Snapshot.taken_at` (wall time) for the age check."""

    def admit(self, action: Admissible, snapshot: Snapshot) -> tuple[Element, Any]:
        element = snapshot.element(action.element_id)
        handle = snapshot.handle(action.element_id)
        if not snapshot.stable:
            raise RefusalError(ErrorCode.UNSTABLE_UI, "snapshot_unstable")
        if self.clock() - snapshot.taken_at > SNAPSHOT_MAX_AGE_S:
            raise RefusalError(ErrorCode.UNSTABLE_UI, "snapshot_stale")
        if not element.enabled:
            raise RefusalError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "disabled")
        if element.secure and action.approval is None:
            raise RefusalError(ErrorCode.GATE_BLOCKED, "secure_field")
        if not self.windows.is_window(snapshot.hwnd):
            raise RefusalError(ErrorCode.APP_CRASHED, "window_gone")
        cloaked = self.windows.is_cloaked(snapshot.hwnd)
        if cloaked is True:
            raise RefusalError(ErrorCode.WINDOW_OTHER_DESKTOP, "window_cloaked")
        if cloaked is not False:
            # DWM did not answer: unclear, never visible (issue #48)
            raise RefusalError(ErrorCode.WINDOW_OTHER_DESKTOP, "cloak_unreadable")
        if not self.windows.is_window_enabled(snapshot.hwnd):
            raise RefusalError(ErrorCode.INTERRUPT_MODAL, "window_disabled")
        type_id = int(handle.CurrentControlType)
        role = self.bindings.control_types.get(type_id, str(type_id))
        name = normalise_name(str(handle.CurrentName or ""))
        if role != element.role or name != normalise_name(element.name):
            raise RefusalError(ErrorCode.ELEMENT_NOT_FOUND, "identity_changed")
        if action.postcondition is not None and action.postcondition():
            raise RefusalError(ErrorCode.VERIFY_FAILED, "precondition_already_satisfied")
        return element, handle


def code_for(exc: Exception) -> tuple[ErrorCode | None, dict[str, Any]]:
    """Taxonomy code and whitelisted evidence for an exception; None for a programming error."""
    if isinstance(exc, SteerError):
        extra = {"detail": exc.detail} if isinstance(exc, RefusalError) else {}
        return exc.code, extra
    hresult = getattr(exc, "hresult", None)  # _ctypes.COMError; comtypes has no stubs
    if isinstance(hresult, int):
        unsigned = hresult & 0xFFFFFFFF
        code = HRESULT_CODES.get(unsigned, ErrorCode.PERCEPTION_TIMEOUT)
        return code, {"hresult": f"0x{unsigned:08X}"}
    if isinstance(exc, OSError):
        return ErrorCode.PERCEPTION_TIMEOUT, {}
    return None, {}


def classify(call: Call, exc: Exception) -> ActionResult | None:
    """Phase-tracked failure classification; None means re-raise (bug before anything was sent)."""
    code, extra = code_for(exc)
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
        call.steps.append(Step(label=call.sent, mechanism=call.mechanism, outcome="failed"))
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
