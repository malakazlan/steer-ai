"""UIA cached snapshot: one cross-process round trip (CacheRequest + FindAllBuildCache), timed.

The design (section 5.6.2) mandates: MTA apartment, connection and transaction timeouts set,
one cached FindAll instead of node-by-node walking. This module is the measuring instrument.
comtypes is imported lazily so the module can be collected on non-Windows CI runners.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Any

from steerai.probes.win_input import ensure_per_monitor_v2

_COINIT_MULTITHREADED = 0x0
_CACHED_PROPERTIES = (
    "UIA_NamePropertyId",
    "UIA_ControlTypePropertyId",
    "UIA_AutomationIdPropertyId",
    "UIA_BoundingRectanglePropertyId",
    "UIA_IsEnabledPropertyId",
    "UIA_IsOffscreenPropertyId",
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


@dataclass(frozen=True, slots=True)
class TreeSnapshot:
    nodes: list[RawNode]
    fetch_ms: float
    walk_ms: float


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
        self._control_type_names = _control_type_names(self._uia_module)

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
        root = element.BuildUpdatedCache(cache_request)
        fetch_ms = (time.perf_counter_ns() - started) / 1_000_000
        started = time.perf_counter_ns()
        nodes: list[RawNode] = []
        self._walk_cached(root, 0, nodes)
        walk_ms = (time.perf_counter_ns() - started) / 1_000_000
        return TreeSnapshot(nodes=nodes, fetch_ms=fetch_ms, walk_ms=walk_ms)

    def _walk_cached(self, element: Any, depth: int, out: list[RawNode]) -> None:
        # `element` is a cached COM element; comtypes has no stubs, hence Any.
        rect = element.CachedBoundingRectangle
        type_id = int(element.CachedControlType)
        out.append(
            RawNode(
                depth=depth,
                role=self._control_type_names.get(type_id, str(type_id)),
                name=str(element.CachedName or ""),
                automation_id=str(element.CachedAutomationId or ""),
                box=(int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)),
                offscreen=bool(element.CachedIsOffscreen),
            )
        )
        try:
            children = element.GetCachedChildren()
            count = int(children.Length)
        except ValueError:  # comtypes raises on a NULL pointer: leaf node
            return
        for index in range(count):
            self._walk_cached(children.GetElement(index), depth + 1, out)

    def bounding_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        """UIA bounding rectangle of the window element, in physical screen pixels."""
        try:
            rect = self._uia.ElementFromHandle(hwnd).CurrentBoundingRectangle
        except Exception as exc:  # COMError exists only once comtypes is loaded
            raise OSError(f"no UIA element for hwnd {hwnd:#x}: {exc}") from exc
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)

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


def _create_client() -> tuple[Any, Any]:
    # Returns (generated UIAutomationClient module, IUIAutomation2 instance); both untyped COM.
    if "comtypes" not in sys.modules:
        # Must be set before comtypes initialises COM on this thread: MTA per design 5.6.2.
        sys.coinit_flags = _COINIT_MULTITHREADED  # type: ignore[attr-defined]  # comtypes reads it
    # Lazy imports: comtypes is Windows-only and must not load at module import time.
    import comtypes.client  # noqa: PLC0415

    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient  # noqa: PLC0415

    client = comtypes.client.CreateObject(
        UIAutomationClient.CUIAutomation8, interface=UIAutomationClient.IUIAutomation2
    )
    return UIAutomationClient, client


def _control_type_names(uia_module: Any) -> dict[int, str]:
    # `uia_module` is the comtypes-generated module; untyped, hence Any.
    suffix = "ControlTypeId"
    return {
        int(getattr(uia_module, attr)): attr.removeprefix("UIA_").removesuffix(suffix)
        for attr in dir(uia_module)
        if attr.startswith("UIA_") and attr.endswith(suffix)
    }
