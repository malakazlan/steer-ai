"""[V2.21] #9 field-owned: whether an element belongs to a typed field (its suggestions, its own
list), so it never proves that a search happened. One pure function for the step-3 auto proof's
results carrier ([V2.29] Q1) and, later, step 4's `content_present`.

An element is field-owned when any rule holds; every rule that fires is reported, in order
([V2.23]): under the field's ComboBox ancestor (`owned_by_combobox`); named by the field's
ControllerFor when that was read (`owned_by_controller_for`); sharing the field's nearest Search
landmark (`owned_by_search_landmark`); under the field's parent container, unless that parent is
the scope Document or a Main landmark (`owned_by_parent`). Anything merely below the Document is
not. With no landmark map the check fails closed with `landmarks_unread` ([V2.23]): not
"owned", unread.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from steerai.world.effects import Observed, top_document
from steerai.world.elements import Element

OwnedToken = Literal[
    "owned_by_combobox",
    "owned_by_controller_for",
    "owned_by_search_landmark",
    "owned_by_parent",
    "landmarks_unread",
]
COMBOBOX = "ComboBox"


def field_owned(
    element: Element,
    observed: Observed,
    field: tuple[int, ...],
    *,
    controlled: frozenset[tuple[int, ...]] = frozenset(),
) -> tuple[OwnedToken, ...]:
    """The field-owned rules that fire for `element` against the typed `field` in `observed`;
    empty when none does. `controlled` is what the field's ControllerFor names, when read."""
    marks = observed.landmarks
    if marks is None:
        return ("landmarks_unread",)
    snapshot = observed.snapshot
    by_id = {e.id: e for e in snapshot.elements}
    typed = next((e for e in snapshot.elements if e.runtime_id == field), None)
    if typed is None:
        return ()
    tokens: list[OwnedToken] = []
    combo = _combobox_of(typed, by_id)
    if combo is not None and _under(element, combo.id, by_id):
        tokens.append("owned_by_combobox")
    if element.runtime_id in controlled:
        tokens.append("owned_by_controller_for")
    mine, theirs = marks.get(field), marks.get(element.runtime_id)
    if mine is not None and mine.kind == "search" and theirs == mine:
        tokens.append("owned_by_search_landmark")
    if _owned_by_parent(element, typed, by_id, observed):
        tokens.append("owned_by_parent")
    return tuple(tokens)


def _combobox_of(typed: Element, by_id: Mapping[int, Element]) -> Element | None:
    """The field itself when it is a ComboBox, else its nearest ComboBox ancestor."""
    seen: set[int] = set()
    current: Element | None = typed
    while current is not None and current.id not in seen:
        if current.role == COMBOBOX:
            return current
        seen.add(current.id)
        current = None if current.parent_id is None else by_id.get(current.parent_id)
    return None


def _owned_by_parent(
    element: Element,
    typed: Element,
    by_id: Mapping[int, Element],
    observed: Observed,
) -> bool:
    if typed.parent_id is None:
        return False
    parent = by_id.get(typed.parent_id)
    if parent is None:
        return False
    top = top_document(observed.snapshot)
    if parent.role == "Document" or (top is not None and parent.runtime_id == top.runtime_id):
        return False
    nearest = observed.landmarks.get(typed.runtime_id) if observed.landmarks else None
    if nearest is not None and nearest.kind == "main" and nearest.runtime_id == parent.runtime_id:
        return False
    return element.id != typed.id and _under(element, parent.id, by_id)


def _under(element: Element, ancestor: int, by_id: Mapping[int, Element]) -> bool:
    seen: set[int] = set()
    parent = element.parent_id
    while parent is not None and parent not in seen:
        if parent == ancestor:
            return True
        seen.add(parent)
        current = by_id.get(parent)
        parent = None if current is None else current.parent_id
    return False
