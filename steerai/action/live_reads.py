"""The live reads the verifier needs (5.10 `LiveReads`), over the snapshot's COM handles.

This slice reads the Value pattern and the focused element; toggle and selection states are
not read yet (they return None, which the verifier treats as unreadable, never as a value).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from steerai.action.execution import UIA_VALUE_PATTERN, code_for
from steerai.action.sanitize import field_text
from steerai.errors import SteerError
from steerai.world.elements import Element
from steerai.world.model import Snapshot


class _FocusSource(Protocol):
    def focused_runtime_id(self) -> tuple[int, ...]: ...


class _PatternSource(Protocol):
    def pattern(self, handle: Any, pattern_id: int, name: str) -> Any: ...


class UiaLiveReads:
    def __init__(self, client: _FocusSource, bindings: _PatternSource) -> None:
        self._client = client
        self._bindings = bindings

    def value(self, handle: Any) -> str | None:
        """The field's value; a COM failure (a stale handle, a provider timeout) is a taxonomy
        failure the verifier reads as "does not hold" (FV-16); a programming error raises."""
        try:
            pattern = self._bindings.pattern(handle, UIA_VALUE_PATTERN, "ValuePattern")
            raw = pattern.CurrentValue
        except Exception as exc:  # COMError has no stubs: mapped by code_for, or re-raised
            code, _extra = code_for(exc)
            if code is None:
                raise
            raise SteerError(code, "live_read_failed") from exc
        return None if raw is None else str(raw)

    def toggle_state(self, _handle: Any) -> int | None:
        return None

    def is_selected(self, _handle: Any) -> bool | None:
        return None

    def focused_runtime_id(self) -> tuple[int, ...]:
        return self._client.focused_runtime_id()


class _ValueSource(Protocol):
    def value(self, handle: Any) -> str | None: ...  # Any: a comtypes element


def twin_value_of(snapshot: Snapshot, live: _ValueSource | None) -> Callable[[Element], str | None]:
    """A twin field's live value for `world.twins.twin_set`, the one reader for the resolver and
    the conditions (5.8 [v2.52]): a failed read, a snapshot with no handles or a raw None is
    None (unreadable, never a twin set); anything else is the field's text."""

    def read(element: Element) -> str | None:
        if live is None:
            return None
        try:
            raw = live.value(snapshot.handle(element.id))
        except SteerError:
            return None
        return None if raw is None else field_text(str(raw), element.role)

    return read
