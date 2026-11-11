"""UIA cached snapshot: one cross-process round trip (CacheRequest + FindAllBuildCache), timed.

The design (section 5.6.2) mandates: MTA apartment, connection and transaction timeouts set,
one cached FindAll instead of node-by-node walking. This module is the measuring instrument.
comtypes is imported lazily so the module can be collected on non-Windows CI runners.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from steerai.probes.win_input import ensure_per_monitor_v2
from steerai.world.address_bar import DocumentUrl, read_address_bar, read_document_url
from steerai.world.capabilities import Caps, tokens

_COINIT_MULTITHREADED = 0x0
_CACHED_PROPERTIES = (
    "UIA_NamePropertyId",
    "UIA_ControlTypePropertyId",
    "UIA_AutomationIdPropertyId",
    "UIA_BoundingRectanglePropertyId",
    "UIA_IsEnabledPropertyId",
    "UIA_IsOffscreenPropertyId",
    "UIA_RuntimeIdPropertyId",
    "UIA_IsPasswordPropertyId",
    # what the element can do (packet interaction-model-2026-11-12 §1): pattern availability and
    # pattern state, cached in the same fetch; never a value
    "UIA_IsInvokePatternAvailablePropertyId",
    "UIA_IsTogglePatternAvailablePropertyId",
    "UIA_ToggleToggleStatePropertyId",
    "UIA_IsSelectionItemPatternAvailablePropertyId",
    "UIA_SelectionItemIsSelectedPropertyId",
    "UIA_IsExpandCollapsePatternAvailablePropertyId",
    "UIA_ExpandCollapseExpandCollapseStatePropertyId",
    "UIA_IsValuePatternAvailablePropertyId",
    "UIA_ValueIsReadOnlyPropertyId",
    "UIA_IsRangeValuePatternAvailablePropertyId",
)


@dataclass(frozen=True, slots=True)
class Snapshot:
    scope: str
    node_count: int
    elapsed_ms: float
    by_control_type: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RawNode:
    depth: int
    role: str
    name: str
    automation_id: str
    box: tuple[int, int, int, int]
    offscreen: bool
    enabled: bool = True
    runtime_id: tuple[int, ...] = ()
    secure: bool = False
    """Password field: its value is never read and the line is tagged `secure` (WM-23)."""
    caps: tuple[str, ...] = ()
    """What the element can do, as closed tokens (`steerai.world.capabilities`)."""


@dataclass(frozen=True, slots=True)
class TreeSnapshot:
    nodes: list[RawNode]
    fetch_ms: float
    walk_ms: float
    handles: list[Any] = field(default_factory=list)
    """Live cached COM elements, one per node (comtypes has no stubs, hence Any). Act through
    these; they support Current* properties and patterns. Never let them cross a process."""


class UiaClient:
    """Thin, typed boundary around the COM UI Automation client."""

    def __init__(
        self, *, connection_timeout_ms: int = 5000, transaction_timeout_ms: int = 5000
    ) -> None:
        if sys.platform != "win32":
            raise OSError("UI Automation requires Windows")
        # UIA scales rectangles to the client's DPI awareness; set it before the first COM call.
        self.dpi_per_monitor_v2 = ensure_per_monitor_v2()
        self._uia_module, self._uia = _create_client()
        self._uia.ConnectionTimeout = connection_timeout_ms
        self._uia.TransactionTimeout = transaction_timeout_ms
        self._control_type_names = control_type_names(self._uia_module)

    @property
    def connection_timeout_ms(self) -> int:
        return int(self._uia.ConnectionTimeout)

    @property
    def transaction_timeout_ms(self) -> int:
        return int(self._uia.TransactionTimeout)

    def snapshot_desktop_children(self) -> Snapshot:
        root = self._uia.GetRootElement()
        return self._snapshot(root, self._uia_module.TreeScope_Children, "children")

    def snapshot_subtree(self, hwnd: int) -> Snapshot:
        try:
            element = self._uia.ElementFromHandle(hwnd)
        except Exception as exc:  # COMError exists only once comtypes is loaded
            raise OSError(f"no UIA element for hwnd {hwnd:#x}: {exc}") from exc
        return self._snapshot(element, self._uia_module.TreeScope_Subtree, "subtree")

    def cached_tree(self, hwnd: int) -> TreeSnapshot:
        """Whole subtree with structure in one cross-process call (BuildUpdatedCache, Subtree).

        Probe 4 spike: ~36 ms for 227 nodes versus ~500 ms for the flat FindAllBuildCache.
        The walk over cached children is in-process and costs ~1 ms.
        """
        try:
            element = self._uia.ElementFromHandle(hwnd)
        except Exception as exc:  # COMError exists only once comtypes is loaded
            raise OSError(f"no UIA element for hwnd {hwnd:#x}: {exc}") from exc
        cache_request = self._uia.CreateCacheRequest()
        for name in _CACHED_PROPERTIES:
            cache_request.AddProperty(getattr(self._uia_module, name))
        cache_request.TreeScope = self._uia_module.TreeScope_Subtree
        started = time.perf_counter_ns()
        try:
            root = element.BuildUpdatedCache(cache_request)
        except Exception as exc:  # COMError exists only once comtypes is loaded
            raise OSError(f"uia fetch failed for hwnd {hwnd:#x}: {exc}") from exc
        fetch_ms = (time.perf_counter_ns() - started) / 1_000_000
        started = time.perf_counter_ns()
        nodes: list[RawNode] = []
        handles: list[Any] = []
        self._walk_cached(root, 0, nodes, handles)
        walk_ms = (time.perf_counter_ns() - started) / 1_000_000
        return TreeSnapshot(nodes=nodes, fetch_ms=fetch_ms, walk_ms=walk_ms, handles=handles)

    def _walk_cached(
        self, element: Any, depth: int, out: list[RawNode], handles: list[Any]
    ) -> None:
        # `element` is a cached COM element; comtypes has no stubs, hence Any.
        rect = element.CachedBoundingRectangle
        type_id = int(element.CachedControlType)
        runtime_id = element.GetCachedPropertyValue(self._uia_module.UIA_RuntimeIdPropertyId)
        out.append(
            RawNode(
                depth=depth,
                role=self._control_type_names.get(type_id, str(type_id)),
                name=str(element.CachedName or ""),
                automation_id=str(element.CachedAutomationId or ""),
                box=(int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)),
                offscreen=bool(element.CachedIsOffscreen),
                enabled=bool(element.CachedIsEnabled),
                runtime_id=tuple(int(v) for v in runtime_id) if runtime_id else (),
                secure=bool(element.CachedIsPassword),
                caps=tokens(self._caps(element)),
            )
        )
        handles.append(element)
        try:
            children = element.GetCachedChildren()
            count = int(children.Length)
        except ValueError:  # comtypes raises on a NULL pointer: leaf node
            return
        for index in range(count):
            self._walk_cached(children.GetElement(index), depth + 1, out, handles)

    def _caps(self, element: Any) -> Caps:  # Any: a cached comtypes element
        """The cached pattern facts; a state is read only when its pattern is available (an
        absent pattern's state property is UIA's NotSupported value, never a state)."""
        mod = self._uia_module

        def cached(name: str) -> Any:  # Any: a VARIANT value from comtypes
            return element.GetCachedPropertyValue(getattr(mod, name))

        def flag(name: str) -> bool:
            value = cached(name)
            return isinstance(value, bool | int) and bool(value)

        def number(name: str) -> int | None:
            value = cached(name)
            return int(value) if isinstance(value, int) and not isinstance(value, bool) else None

        def boolean(name: str) -> bool | None:
            value = cached(name)
            return bool(value) if isinstance(value, bool | int) else None

        return Caps(
            toggle=(
                number("UIA_ToggleToggleStatePropertyId")
                if flag("UIA_IsTogglePatternAvailablePropertyId")
                else None
            ),
            selected=(
                boolean("UIA_SelectionItemIsSelectedPropertyId")
                if flag("UIA_IsSelectionItemPatternAvailablePropertyId")
                else None
            ),
            expanded=(
                number("UIA_ExpandCollapseExpandCollapseStatePropertyId")
                if flag("UIA_IsExpandCollapsePatternAvailablePropertyId")
                else None
            ),
            read_only=(
                boolean("UIA_ValueIsReadOnlyPropertyId")
                if flag("UIA_IsValuePatternAvailablePropertyId")
                else None
            ),
            invoke=flag("UIA_IsInvokePatternAvailablePropertyId"),
            range_value=flag("UIA_IsRangeValuePatternAvailablePropertyId"),
        )

    def bounding_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        """UIA bounding rectangle of the window element, in physical screen pixels."""
        try:
            rect = self._uia.ElementFromHandle(hwnd).CurrentBoundingRectangle
        except Exception as exc:  # COMError exists only once comtypes is loaded
            raise OSError(f"no UIA element for hwnd {hwnd:#x}: {exc}") from exc
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)

    def runtime_id_at(self, x: int, y: int) -> tuple[int, ...]:
        """RuntimeId of the UIA element under a screen pixel (`ElementFromPoint`). Off every
        monitor UIA answers the desktop root (measured: RuntimeId (42, <desktop hwnd>)), never
        nothing.
        Hit testing through UIA sees in-window covering (popups, overlays) that the Win32
        root-window check cannot."""
        try:
            element = self._uia.ElementFromPoint(self._uia_module.tagPOINT(x, y))
        except Exception as exc:  # COMError exists only once comtypes is loaded
            raise OSError(f"no UIA element at ({x}, {y}): {exc}") from exc
        if not element:
            return ()
        runtime_id = element.GetRuntimeId()
        return tuple(int(v) for v in runtime_id) if runtime_id else ()

    def address_bar(self, hwnd: int) -> str | None:
        """Chrome's address bar of the window (`steerai.world.address_bar`), None when there is
        no one omnibox out of focus with a value."""
        try:
            return read_address_bar(self._uia, self._uia_module, hwnd)
        except Exception as exc:  # COMError exists only once comtypes is loaded
            raise OSError(f"address bar unreadable: {exc}") from exc

    def document_url(self, hwnd: int) -> DocumentUrl | None:
        """The URL of the window's one top-level Document with its RuntimeId
        (`steerai.world.address_bar`)."""
        try:
            return read_document_url(self._uia, self._uia_module, hwnd)
        except Exception as exc:  # COMError exists only once comtypes is loaded (see #160)
            raise OSError(f"document url unreadable: {exc}") from exc

    def focused_runtime_id(self) -> tuple[int, ...]:
        """RuntimeId of the element with keyboard focus (`GetFocusedElement`), empty when none."""
        try:
            element = self._uia.GetFocusedElement()
        except Exception as exc:  # COMError exists only once comtypes is loaded
            raise OSError(f"no focused UIA element: {exc}") from exc
        if not element:
            return ()
        runtime_id = element.GetRuntimeId()
        return tuple(int(v) for v in runtime_id) if runtime_id else ()

    def cached_list_items(self, element: Any, properties: Sequence[int]) -> list[Any]:
        """The ListItems among `element`'s descendants with `properties` cached, in one
        FindAllBuildCache (a ComboBox's options, W4-13). `element` is a COM element (Any); a COM
        failure propagates unchanged so the executor's `classify` reads its HRESULT."""
        cache_request = self._uia.CreateCacheRequest()
        for property_id in properties:
            cache_request.AddProperty(property_id)
        condition = self._uia.CreatePropertyCondition(
            self._uia_module.UIA_ControlTypePropertyId, self._uia_module.UIA_ListItemControlTypeId
        )
        found = element.FindAllBuildCache(
            self._uia_module.TreeScope_Descendants, condition, cache_request
        )
        if not found:
            return []
        return [found.GetElement(index) for index in range(int(found.Length))]

    def _snapshot(self, element: Any, scope: int, scope_name: str) -> Snapshot:
        # `element` is a COM interface pointer; comtypes has no type stubs, hence Any.
        cache_request = self._uia.CreateCacheRequest()
        for name in _CACHED_PROPERTIES:
            cache_request.AddProperty(getattr(self._uia_module, name))
        condition = self._uia.CreateTrueCondition()
        started = time.perf_counter_ns()
        found = element.FindAllBuildCache(scope, condition, cache_request)
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        count = int(found.Length)
        by_type: dict[str, int] = {}
        for index in range(count):
            type_id = int(found.GetElement(index).CachedControlType)
            name = self._control_type_names.get(type_id, str(type_id))
            by_type[name] = by_type.get(name, 0) + 1
        return Snapshot(
            scope=scope_name, node_count=count, elapsed_ms=elapsed_ms, by_control_type=by_type
        )


def load_uia_module() -> Any:
    """The comtypes-generated `UIAutomationClient` module (untyped COM, Windows only).

    Sets the MTA flag before comtypes initialises COM on this thread (design 5.6.2), so every
    user of the module, client or executor, goes through here.
    """
    if "comtypes" not in sys.modules:
        sys.coinit_flags = _COINIT_MULTITHREADED  # type: ignore[attr-defined]  # comtypes reads it
    # Lazy imports: comtypes is Windows-only and must not load at module import time.
    import comtypes.client  # noqa: PLC0415

    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient  # noqa: PLC0415

    return UIAutomationClient


def _create_client() -> tuple[Any, Any]:
    # Returns (generated UIAutomationClient module, IUIAutomation2 instance); both untyped COM.
    import comtypes.client  # noqa: PLC0415

    UIAutomationClient = load_uia_module()  # noqa: N806 - module name
    client = comtypes.client.CreateObject(
        UIAutomationClient.CUIAutomation8, interface=UIAutomationClient.IUIAutomation2
    )
    return UIAutomationClient, client


def control_type_names(uia_module: Any) -> dict[int, str]:
    """UIA control type id -> role name (`UIA_ButtonControlTypeId` -> `Button`)."""
    # `uia_module` is the comtypes-generated module; untyped, hence Any.
    suffix = "ControlTypeId"
    return {
        int(getattr(uia_module, attr)): attr.removeprefix("UIA_").removesuffix(suffix)
        for attr in dir(uia_module)
        if attr.startswith("UIA_") and attr.endswith(suffix)
    }
