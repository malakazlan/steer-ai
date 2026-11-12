"""A ComboBox whose options live outside its subtree (gallery g14, Chrome 156): an ARIA combobox
(`role=combobox` on a readonly input) opens a separate `role=listbox` named by `aria-controls`,
which Core-AAM maps to UIA ControllerFor. Measured: ControllerFor is empty while the list is
hidden and names the List once Expand opened it; the options are ListItems with SelectionItem.
The option source reads the ComboBox's own subtree first, then the elements ControllerFor names."""

from __future__ import annotations

from typing import Any

from steerai.action.options import UiaOptions


class _Item:
    def __init__(self, name: str) -> None:
        self.CachedName = name
        self.CachedIsEnabled = True

    def GetCachedPropertyValue(self, _property_id: int) -> bool:  # noqa: N802 - the COM name
        return True


class _Array:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    @property
    def Length(self) -> int:  # noqa: N802 - the COM name
        return len(self.items)

    def GetElement(self, index: int) -> Any:  # noqa: N802
        return self.items[index]


class _Element:
    def __init__(self, items: list[_Item], controls: list[_Element] | None = None) -> None:
        self.items = items
        self.controls = controls or []

    @property
    def CurrentControllerFor(self) -> _Array:  # noqa: N802 - the COM name
        return _Array(list(self.controls))


class _Client:
    def cached_list_items(self, element: _Element, _properties: Any) -> list[_Item]:
        return list(element.items)


def test_the_combobox_s_own_options_come_first() -> None:
    combo = _Element([_Item("Canada"), _Item("Kenya")], controls=[_Element([_Item("Elsewhere")])])
    assert [o.name for o in UiaOptions(_Client()).list_items(combo)] == ["Canada", "Kenya"]


def test_options_in_a_controlled_list_are_found_when_the_subtree_has_none() -> None:
    listbox = _Element([_Item("Red"), _Item("Teal")])
    combo = _Element([], controls=[listbox])
    assert [o.name for o in UiaOptions(_Client()).list_items(combo)] == ["Red", "Teal"]


def test_no_options_anywhere_is_an_empty_list() -> None:
    assert UiaOptions(_Client()).list_items(_Element([], controls=[])) == []
