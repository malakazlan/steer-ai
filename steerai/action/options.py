"""A ComboBox's options as `choose` reads them (widget-verbs W4 v1-v3).

Chrome 156 puts a native select's options in the ComboBox's own subtree only after Expand (probe
`widget_patterns --section tree`, n=5; ControllerFor has 0 targets), so the source is asked for
the ListItems under the ComboBox's own element, never the window root (WV-05, W4-13). One cached
round trip per read: the name, IsEnabled and whether SelectionItem is available, so a long list
is not read item by item. A COM failure propagates to the executor's `classify`.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from steerai.world.model import normalise_name

UIA_NAME_PROPERTY = 30005
UIA_IS_ENABLED_PROPERTY = 30010
UIA_IS_SELECTION_ITEM_AVAILABLE_PROPERTY = 30036
"""UIAutomationClient property ids (UIA_*PropertyId), kept as literals like the pattern ids."""


MAX_OPTION_UNITS = 500
"""An option is a ComboBox value: the `value` condition's cap (UTF-16 units) holds for it too."""
_REJECTED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def option_key(text: str) -> str:
    """The one comparator for a choose (W4-05, case folded by the PR #192 review): NFKC (a
    no-break space folds to a space), whitespace folded, case folded. It serves option
    resolution, already-equal, the readback, the choose template, a choose-proven `value` and
    the option's verbatim check against the goal; `type` and its `value` stay exact."""
    return normalise_name(text).casefold()


def clean_option(option: str) -> str:
    """The option as the plan wrote it, NFKC and whitespace folded, case kept for the ask; it is
    compared through `option_key`. Empty, over `MAX_OPTION_UNITS`, or holding a control, format,
    surrogate, private-use or unassigned character (a hidden word, RN-04) is refused."""
    folded = normalise_name(option)
    if not folded:
        raise ValueError("choose needs a non-empty option")
    if len(folded.encode("utf-16-le", "surrogatepass")) // 2 > MAX_OPTION_UNITS:
        raise ValueError(f"the option is over {MAX_OPTION_UNITS} UTF-16 units")
    if any(unicodedata.category(ch) in _REJECTED_CATEGORIES for ch in folded):
        raise ValueError("the option holds a control or invisible character")
    return folded


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
    """The ListItems in a ComboBox's subtree through one cached FindAll (`UiaClient`); when the
    subtree holds none, the ListItems of the elements its ControllerFor names: an ARIA combobox
    opens a separate listbox named by `aria-controls` (W3C Core-AAM: ControllerFor), measured on
    the gallery (g14, Chrome 156: empty while the list is hidden, the List once Expand opened
    it)."""

    def __init__(self, client: _Descendants) -> None:
        self._client = client

    def list_items(self, combo: Any) -> list[OptionItem]:
        properties = (
            UIA_NAME_PROPERTY,
            UIA_IS_ENABLED_PROPERTY,
            UIA_IS_SELECTION_ITEM_AVAILABLE_PROPERTY,
        )
        found = self._client.cached_list_items(combo, properties)
        if not found:
            controlled = combo.CurrentControllerFor
            for index in range(int(controlled.Length) if controlled else 0):
                found = [
                    *found,
                    *self._client.cached_list_items(controlled.GetElement(index), properties),
                ]
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
