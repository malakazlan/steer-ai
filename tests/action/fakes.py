"""Fakes shared by the executor tests: a COM-like element handle, a window-state stub, a
COMError-shaped exception and the control-type table the executors are given in pure tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from steerai.action.keyboard import GateApproval

BOX = (10, 10, 110, 50)
UIA_INVOKE, UIA_VALUE, UIA_TOGGLE, UIA_EXPAND, UIA_SELECTION_ITEM, UIA_SCROLL_ITEM = (
    10000,
    10002,
    10015,
    10005,
    10010,
    10017,
)
# UIA_*ControlTypeId values; the executor takes the mapping from the comtypes module on Windows
# and from this table in pure tests.
TYPE_IDS = {
    "Button": 50000,
    "CheckBox": 50002,
    "Edit": 50004,
    "ListItem": 50007,
    "TreeItem": 50024,
    "Document": 50030,
}
E_ACCESSDENIED = 0x80070005
UIA_E_ELEMENTNOTENABLED = 0x80040200
UIA_E_ELEMENTNOTAVAILABLE = 0x80040201
RPC_S_SERVER_UNAVAILABLE = 0x800706BA
UIA_E_TIMEOUT = 0x80131505
APPROVAL = GateApproval(approved_by="user", reason="login as the user asked", decision_id="d-7")


class ComError(Exception):
    """Shaped like `_ctypes.COMError` (a signed `hresult`); the real type is exercised by the
    Windows dead-handle test."""

    def __init__(self, hresult: int) -> None:
        super().__init__("COM call failed")
        self.hresult = hresult - (1 << 32)


def com_error(hresult: int) -> Exception:
    return ComError(hresult)


@dataclass
class FakeHandle:
    """Behaves like a cached IUIAutomationElement for the patterns the executor uses."""

    value: str | None = ""
    toggled: int = 0  # 0 off, 1 on, 2 indeterminate
    expanded: int = 0  # 0 collapsed, 1 expanded, 2 partially, 3 leaf
    selected: bool = False
    patterns: set[int] = field(default_factory=lambda: {UIA_INVOKE, UIA_VALUE, UIA_TOGGLE})
    invoked: int = 0
    read_only: bool = False
    control_type: int = TYPE_IDS["Button"]
    name: str = "Send"
    offscreen: bool = False
    box: tuple[int, int, int, int] = BOX
    calls: list[str] = field(default_factory=list)

    @property
    def CurrentBoundingRectangle(self) -> Any:  # noqa: N802
        left, top, right, bottom = self.box
        return SimpleNamespace(left=left, top=top, right=right, bottom=bottom)

    @property
    def CurrentControlType(self) -> int:  # noqa: N802 - COM naming
        return self.control_type

    @property
    def CurrentName(self) -> str:  # noqa: N802
        return self.name

    @property
    def CurrentIsOffscreen(self) -> bool:  # noqa: N802
        return self.offscreen

    def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802
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
            def CurrentValue(self) -> str | None:  # noqa: N802
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
                handle.calls.append("expand")

            def Collapse(self) -> None:  # noqa: N802
                handle.expanded = 0
                handle.calls.append("collapse")

            @property
            def CurrentExpandCollapseState(self) -> int:  # noqa: N802
                return handle.expanded

            def Select(self) -> None:  # noqa: N802
                handle.selected = True
                handle.calls.append("select")

            @property
            def CurrentIsSelected(self) -> bool:  # noqa: N802
                return handle.selected

            def ScrollIntoView(self) -> None:  # noqa: N802
                handle.offscreen = False
                handle.calls.append("scroll")

        return _Pattern()


@dataclass
class FakeWindows:
    """Stands in for the Win32 window re-check (probes.windows)."""

    alive: bool = True
    cloaked: bool = False
    enabled: bool = True

    def is_window(self, _hwnd: int) -> bool:
        return self.alive

    def is_cloaked(self, _hwnd: int) -> bool:
        return self.cloaked

    def is_window_enabled(self, _hwnd: int) -> bool:
        return self.enabled
