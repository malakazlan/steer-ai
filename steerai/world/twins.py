"""Twin fields (5.8, design D-150 v2, #150): two page-level copies of one field, which no plan
can tell apart by role and name. Measured 2026-11-08 (probe `twins`, Chrome 156): docs.python.org
shows `Edit "Quick search"` with AutomationId `search-box` in the header Related bar and again
in its footer copy, offscreen, with the same ancestor chain.

One rule for step targets and conditions: `twin_set` says whether a set of matches is such a
pair; which twin may be chosen, and when, is the resolver's (loop) and the condition's (t1).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from steerai.world.elements import Element
from steerai.world.model import Snapshot, normalise_name

PinKey = tuple[int, str, str]
"""(window, role, normalised casefolded name): which twin a verified `type` wrote."""

TWIN_CAP = 2
"""Measured case: two copies. More stays ambiguous until a probe measures it (hunter TW-31)."""
MAX_CHAIN = 64


def _chain(
    element: Element, by_id: dict[int, Element]
) -> tuple[int, tuple[tuple[str, str, str], ...]] | None:
    """The nearest Document above the element (its id) and the (role, name, aid) of every
    ancestor below it; None without a Document above or a chain longer than MAX_CHAIN."""
    links: list[tuple[str, str, str]] = []
    parent = element.parent_id
    for _ in range(MAX_CHAIN):
        if parent is None or parent not in by_id:
            return None
        node = by_id[parent]
        if node.role == "Document":
            return node.id, tuple(links)
        links.append((node.role, node.name, node.automation_id))
        parent = node.parent_id
    return None


def twin_set(matches: Sequence[Element], snapshot: Snapshot) -> bool:
    """Exactly two matches with one shared non-empty AutomationId, neither secure, distinct
    non-empty RuntimeIds, the same nearest Document (never an iframe's copy) and an identical
    ancestor chain up to it (page-level copies, never template rows: hunter TW-01..04, 17)."""
    if len(matches) != TWIN_CAP:
        return False
    first, second = matches
    if not first.automation_id or first.automation_id != second.automation_id:
        return False
    if first.secure or second.secure:
        return False
    if not first.runtime_id or not second.runtime_id or first.runtime_id == second.runtime_id:
        return False
    by_id = {element.id: element for element in snapshot.elements}
    chains = [_chain(first, by_id), _chain(second, by_id)]
    return chains[0] is not None and chains[0] == chains[1]


def on_screen(element: Element, snapshot: Snapshot) -> bool:
    """Not flagged offscreen, with area, and inside the window's box (TW-30)."""
    if element.offscreen or not element.has_area:
        return False
    root = next((e for e in snapshot.elements if e.parent_id is None and e.depth == 0), None)
    if root is None:
        return False
    left, top, right, bottom = element.box
    r_left, r_top, r_right, r_bottom = root.box
    return left >= r_left and top >= r_top and right <= r_right and bottom <= r_bottom


def pin_key(hwnd: int, role: str, name: str) -> PinKey:
    return hwnd, role, normalise_name(name).casefold()


def pinned_twin(
    matches: Sequence[Element], snapshot: Snapshot, pinned: Mapping[PinKey, tuple[int, ...]]
) -> Element | None:
    """The one twin the pin names in this window (D-150 v2 item 4); None when the matches are
    no twin set, nothing is pinned, or the pin names neither."""
    if not twin_set(matches, snapshot):
        return None
    pin = pinned.get(pin_key(snapshot.hwnd, matches[0].role, matches[0].name))
    found = [e for e in matches if pin and e.runtime_id == pin]
    return found[0] if len(found) == 1 else None
