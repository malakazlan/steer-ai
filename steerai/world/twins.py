"""Twin fields (5.8, design D-150 v2, #150): two page-level copies of one field, which no plan
can tell apart by role and name. Measured 2026-11-08 (probe `twins`, Chrome 156): docs.python.org
shows `Edit "Quick search"` with AutomationId `search-box` in the header Related bar and again
in its footer copy, offscreen, with the same ancestor chain.

One rule for step targets and conditions: `twin_set` says whether a set of matches is such a
pair; which twin may be chosen, and when, is the resolver's (loop) and the condition's (t1).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

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


ValueOf = Callable[[Element], str | None]
"""A twin's live value; None when it cannot be read."""


def twin_set(
    matches: Sequence[Element], snapshot: Snapshot, value_of: ValueOf | None = None
) -> bool:
    """Exactly two matches with one shared non-empty AutomationId, neither secure, distinct
    non-empty RuntimeIds, the same nearest Document (never an iframe's copy) and an identical
    ancestor chain up to it (page-level copies, never template rows: hunter TW-01..04, 17).
    A chain that differs only because an ancestor is named from the field's own typed text
    (Sphinx's list item: "Go|" becomes "pathlibGo|") still matches through `value_of`
    (5.8 [v2.52], `_typed_match`)."""
    if len(matches) != TWIN_CAP or not _same_kind(*matches):
        return False
    first, second = matches
    by_id = {element.id: element for element in snapshot.elements}
    chains = [_chain(first, by_id), _chain(second, by_id)]
    if chains[0] is None or chains[1] is None or chains[0][0] != chains[1][0]:
        return False
    if chains[0] == chains[1]:
        return True
    return value_of is not None and _typed_match(
        (first, chains[0][1]), (second, chains[1][1]), value_of
    )


def named_pair(matches: Sequence[Element], snapshot: Snapshot) -> bool:
    """Two same-named fields that are not copies (TW-45; H4 dr1 and deb1, packet
    h4-2026-11-11): exactly two, same role and normalised name, neither secure, distinct
    non-empty RuntimeIds, automation ids distinct or both empty (one shared id is `twin_set`'s
    copy), the same nearest Document; the ancestor chains may differ. Distinct ids mean
    distinct forms (docs.rs `nav-search` / `search`; Debian `kw` names packages, `keyword`
    file contents), so position never tells function: the class exists so the loop asks,
    never picks."""
    if len(matches) != TWIN_CAP:
        return False
    first, second = matches
    if (
        first.role != second.role
        or normalise_name(first.name).casefold() != normalise_name(second.name).casefold()
        or first.secure
        or second.secure
        or not first.runtime_id
        or not second.runtime_id
        or first.runtime_id == second.runtime_id
        or (first.automation_id and first.automation_id == second.automation_id)
    ):
        return False
    by_id = {element.id: element for element in snapshot.elements}
    chains = [_chain(first, by_id), _chain(second, by_id)]
    return chains[0] is not None and chains[1] is not None and chains[0][0] == chains[1][0]


def _same_kind(first: Element, second: Element) -> bool:
    """One shared non-empty AutomationId, neither secure, distinct non-empty RuntimeIds."""
    return (
        bool(first.automation_id)
        and first.automation_id == second.automation_id
        and not first.secure
        and not second.secure
        and bool(first.runtime_id)
        and bool(second.runtime_id)
        and first.runtime_id != second.runtime_id
    )


Chain = tuple[tuple[str, str, str], ...]


def _typed_match(
    first: tuple[Element, Chain], second: tuple[Element, Chain], value_of: ValueOf
) -> bool:
    """Exactly one twin holds a non-empty live value; with its first occurrence set aside in
    each ancestor name of that twin's own chain, the chains match after `normalise_name`. Two
    values or an unreadable value never match. Only one occurrence is set aside: a name holding
    the value twice still differs, and a label before the field holding the value fails closed
    (review of #172 and #174)."""
    values = [value_of(first[0]), value_of(second[0])]
    if values[0] is None or values[1] is None or bool(values[0]) == bool(values[1]):
        return False
    typed, plain = (first, second) if values[0] else (second, first)
    value = values[0] or values[1] or ""
    aside = tuple((role, name.replace(value, "", 1), aid) for role, name, aid in typed[1])
    return _normalised(aside) == _normalised(plain[1])


def _normalised(chain: Chain) -> Chain:
    return tuple((role, normalise_name(name), aid) for role, name, aid in chain)


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
    matches: Sequence[Element],
    snapshot: Snapshot,
    pinned: Mapping[PinKey, tuple[int, ...]],
    value_of: ValueOf | None = None,
) -> Element | None:
    """The one twin the pin names in this window (D-150 v2 item 4), for copies and for a named
    pair (TW-45; the `value` condition reads the field the person chose and the verified type
    pinned, packet h4-2026-11-11 round 2 item 6); None when the matches are neither, nothing is
    pinned, or the pin names neither."""
    if not twin_set(matches, snapshot, value_of) and not named_pair(matches, snapshot):
        return None
    pin = pinned.get(pin_key(snapshot.hwnd, matches[0].role, matches[0].name))
    found = [e for e in matches if pin and e.runtime_id == pin]
    return found[0] if len(found) == 1 else None
