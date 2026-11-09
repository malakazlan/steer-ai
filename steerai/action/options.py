"""A ComboBox's options as `choose` reads them (widget-verbs W4 v1-v3).

Chrome 156 puts a native select's options in the ComboBox's own subtree only after Expand (probe
`widget_patterns --section tree`, n=5; ControllerFor has 0 targets), so the source is asked for
the ListItems under the ComboBox's own element, never the window root (WV-05, W4-13). One cached
round trip per read: the name, IsEnabled and whether SelectionItem is available, so a long list
is not read item by item. A COM failure propagates to the executor's `classify`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

UIA_NAME_PROPERTY = 30005
UIA_IS_ENABLED_PROPERTY = 30010
UIA_IS_SELECTION_ITEM_AVAILABLE_PROPERTY = 30036
"""UIAutomationClient property ids (UIA_*PropertyId), kept as literals like the pattern ids."""


@dataclass(frozen=True, slots=True)
class OptionItem:
    """One ListItem under a ComboBox; its name is screen text and stays out of the repr."""

    name: str = field(repr=False)
    enabled: bool
    selectable: bool
    """It exposes SelectionItemPattern."""
    handle: Any = field(repr=False)
    """The live COM element (comtypes has no stubs, hence Any)."""


class OptionSource(Protocol):
    def list_items(self, combo: Any) -> Sequence[OptionItem]: ...


class _Descendants(Protocol):
    def cached_list_items(self, element: Any, properties: Sequence[int]) -> list[Any]: ...


class UiaOptions:
    """The ListItems in a ComboBox's subtree through one cached FindAll (`UiaClient`)."""

    def __init__(self, client: _Descendants) -> None:
        self._client = client

    def list_items(self, combo: Any) -> list[OptionItem]:
        found = self._client.cached_list_items(
            combo,
            (UIA_NAME_PROPERTY, UIA_IS_ENABLED_PROPERTY, UIA_IS_SELECTION_ITEM_AVAILABLE_PROPERTY),
        )
        return [
            OptionItem(
                name=str(element.CachedName or ""),
                enabled=bool(element.CachedIsEnabled),
                selectable=bool(
                    element.GetCachedPropertyValue(UIA_IS_SELECTION_ITEM_AVAILABLE_PROPERTY)
                ),
                handle=element,
            )
            for element in found
        ]
