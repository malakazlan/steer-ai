"""The live reads the verifier needs (5.10 `LiveReads`), over the snapshot's COM handles.

This slice reads the Value pattern and the focused element; toggle and selection states are
not read yet (they return None, which the verifier treats as unreadable, never as a value).
"""

from __future__ import annotations

from typing import Any, Protocol

from steerai.action.execution import UIA_VALUE_PATTERN


class _FocusSource(Protocol):
    def focused_runtime_id(self) -> tuple[int, ...]: ...


class _PatternSource(Protocol):
    def pattern(self, handle: Any, pattern_id: int, name: str) -> Any: ...


class UiaLiveReads:
    def __init__(self, client: _FocusSource, bindings: _PatternSource) -> None:
        self._client = client
        self._bindings = bindings

    def value(self, handle: Any) -> str | None:
        pattern = self._bindings.pattern(handle, UIA_VALUE_PATTERN, "ValuePattern")
        raw = pattern.CurrentValue
        return None if raw is None else str(raw)

    def toggle_state(self, _handle: Any) -> int | None:
        return None

    def is_selected(self, _handle: Any) -> bool | None:
        return None

    def focused_runtime_id(self) -> tuple[int, ...]:
        return self._client.focused_runtime_id()
