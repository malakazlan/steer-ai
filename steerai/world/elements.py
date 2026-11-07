"""Structured element list: the text a decision model sees (design 5.7, 6.2).

Each line: id, indentation by depth, role, quoted name, coarse box, optional `disabled` / `secure`
tags. Names are one line (control characters escaped) and capped head-and-tail so trailing
discriminators survive (edge cases CC-10, CC-24, WM-19). Off-screen and zero-area elements are
dropped (WM-27); disabled interactive elements are kept and tagged (CC-25); the nearest ancestor of
every kept element is kept too, even when unnamed, so "which Delete belongs to which row" has an
answer (WM-18). Password fields never carry a value and are tagged `secure` (WM-23).
"""

from __future__ import annotations

from collections.abc import Sequence
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

    @property
    def has_area(self) -> bool:
        return self.box[2] > self.box[0] and self.box[3] > self.box[1]


def _wanted(element: Element) -> bool:
    return (
        not element.offscreen
        and element.has_area
        and (element.role in INTERACTIVE_ROLES or bool(element.name))
    )


def prune(elements: Sequence[Element], *, cap: int) -> list[Element]:
    """Keep interactive and named elements plus each kept element's nearest ancestor; cap in order.

    Ancestors are kept even when unnamed so grouping survives. Off-screen and zero-area elements
    are never kept. `cap` must be >= 1.
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
    kept = [element for element in elements if element.id in keep]
    return kept[:cap]


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
        lines.append(line)
    if remaining > 0:
        lines.append(f"(... {remaining} more not shown)")
    return lines
