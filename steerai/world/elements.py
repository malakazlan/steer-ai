"""Structured element list: the text a decision model sees (design 5.7, 6.2).

Each line: id, indentation by depth, role, quoted name, coarse box, optional `disabled` / `secure`
tags. Names are one line (control characters escaped) and capped head-and-tail so trailing
discriminators survive (edge cases CC-10, CC-24, WM-19). Off-screen and zero-area elements are
dropped (WM-27); disabled interactive elements are kept and tagged (CC-25); the nearest ancestor of
every kept element is kept too, even when unnamed, so "which Delete belongs to which row" has an
answer (WM-18). Password fields never carry a value and are tagged `secure` (WM-23).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

Box = tuple[int, int, int, int]
MAX_NAME_CHARS = 120
_HEAD_CHARS = 80

INTERACTIVE_ROLES = frozenset(
    {
        "Button",
        "CheckBox",
        "ComboBox",
        "DataItem",
        "Edit",
        "Hyperlink",
        "ListItem",
        "MenuItem",
        "RadioButton",
        "Slider",
        "Spinner",
        "SplitButton",
        "TabItem",
        "TreeItem",
    }
)


@dataclass(frozen=True, slots=True)
class Element:
    id: int
    depth: int
    role: str
    name: str
    box: Box
    offscreen: bool = False
    enabled: bool = True
    automation_id: str = ""
    runtime_id: tuple[int, ...] = ()
    parent_id: int | None = None
    secure: bool = False
    caps: tuple[str, ...] = ()
    """What the element can do, as closed tokens (`steerai.world.capabilities`)."""

    @property
    def has_area(self) -> bool:
        return self.box[2] > self.box[0] and self.box[3] > self.box[1]


def outermost(matches: Sequence[Element], elements: Sequence[Element]) -> list[Element]:
    """The matches that are not inside another match: a grid row and its cell carry one name as
    two DataItems (gallery, Chrome 156); the cell is part of the row, and the row is what a
    `select` acts on and a `selected` condition reads. A TreeItem inside a same-named TreeItem
    is another item (a folder `src` in a folder `src`), never folded ([v2.81], TE-02)."""
    by_id = {e.id: e for e in elements}
    ids = {e.id for e in matches}

    def nested(element: Element) -> bool:
        if element.role == "TreeItem":
            return False
        parent = element.parent_id
        while parent is not None:
            if parent in ids:
                return True
            parent = by_id[parent].parent_id if parent in by_id else None
        return False

    return [e for e in matches if not nested(e)]


def _wanted(element: Element) -> bool:
    return (
        not element.offscreen
        and element.has_area
        and (element.role in INTERACTIVE_ROLES or bool(element.name))
    )


def prune(
    elements: Sequence[Element], *, cap: int, offscreen: bool = False, minimized: bool = False
) -> list[Element]:
    """Keep interactive and named elements plus each kept element's nearest ancestor; cap in order.

    Ancestors are kept even when unnamed so grouping survives. Zero-area elements are never kept.
    With `offscreen`, the cap's room after every on-screen line goes to offscreen elements a
    scroll can reach (D-OS v2): chosen nearest the window first, printed in tree order after the
    on-screen lines, each with its nearest ancestor; none from a `minimized` window. `cap` must
    be >= 1.
    """
    if cap < 1:
        raise ValueError("cap must be >= 1")
    by_id = {element.id: element for element in elements}
    keep: set[int] = set()
    for element in elements:
        if _wanted(element):
            keep.add(element.id)
            if element.parent_id is not None and element.parent_id in by_id:
                parent = by_id[element.parent_id]
                if not parent.offscreen:
                    keep.add(parent.id)
    kept = [element for element in elements if element.id in keep][:cap]
    if not offscreen or minimized or len(kept) >= cap:
        return kept
    root = next((e for e in elements if e.parent_id is None and e.depth == 0), None)
    if root is None:
        return kept
    chosen = _reachable(elements, by_id, root.box, {e.id for e in kept}, cap - len(kept))
    return kept + [element for element in elements if element.id in chosen]


def _reachable(
    elements: Sequence[Element],
    by_id: Mapping[int, Element],
    window: Box,
    kept: set[int],
    room: int,
) -> set[int]:
    """Offscreen wanted elements whose box overlaps the window horizontally (the classes a
    ScrollIntoView brings on screen 5/5; sr-only text at x=-12440 never does, probe
    `offscreen_reach`), nearest first by vertical distance, each with its nearest ancestor when
    that is not listed yet; an element whose ancestor does not fit is left out."""
    candidates = [e for e in elements if e.offscreen and _listable(e) and _overlaps(e.box, window)]
    candidates.sort(key=lambda e: _distance(e.box, window))
    chosen: set[int] = set()
    for element in candidates:
        if element.id in chosen:
            continue
        parent = by_id.get(element.parent_id) if element.parent_id is not None else None
        group = {element.id}
        if parent is not None and parent.has_area and parent.id not in kept | chosen:
            group.add(parent.id)
        if len(chosen) + len(group) > room:
            continue
        chosen |= group
    return chosen


def _listable(element: Element) -> bool:
    return element.has_area and (element.role in INTERACTIVE_ROLES or bool(element.name))


def _overlaps(box: Box, window: Box) -> bool:
    return box[0] < window[2] and box[2] > window[0]


def _distance(box: Box, window: Box) -> int:
    """Vertical distance from the window's box; 0 inside its rows."""
    if box[3] <= window[1]:
        return window[1] - box[3]
    if box[1] >= window[3]:
        return box[1] - window[3]
    return 0


def safe_name(name: str) -> str:
    """One printable line, capped head and tail so the end of a long name is still visible.
    A backslash and a double quote are escaped too: a name can never close its quoted field
    and forge a listing row (IJ-37, #87)."""
    quoted = name.replace("\\", "\\\\").replace('"', "\\u0022")
    escaped = "".join(
        ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii") for ch in quoted
    )
    if len(escaped) <= MAX_NAME_CHARS:
        return escaped
    tail = MAX_NAME_CHARS - _HEAD_CHARS - 1
    return escaped[:_HEAD_CHARS] + "…" + escaped[-tail:]


def format_lines(elements: Sequence[Element], *, remaining: int = 0) -> list[str]:
    lines = []
    for element in elements:
        line = (
            f'{element.id} {"  " * element.depth}{element.role} "{safe_name(element.name)}" '
            f"[{element.box[0]},{element.box[1]},{element.box[2]},{element.box[3]}]"
        )
        if not element.enabled:
            line += " disabled"
        if element.secure:
            line += " secure"
        if element.offscreen:
            line += " offscreen"
        for token in element.caps:
            line += f" {token}"
        lines.append(line)
    if remaining > 0:
        lines.append(f"(... {remaining} more not shown)")
    return lines
