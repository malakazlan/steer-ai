"""Structured element list: the text a decision model sees (design 5.7, 6.2).

Each line: id, indentation by depth, role, quoted name, coarse box, optional `disabled` tag.
Names are one line (control characters escaped) and length-capped so a name can never forge a
second line or bloat the prompt (edge cases CC-10, CC-24). Off-screen elements are dropped;
disabled interactive elements are kept and tagged so the chooser can avoid them (CC-25).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

Box = tuple[int, int, int, int]
MAX_NAME_CHARS = 120

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


def prune(elements: Sequence[Element], *, cap: int) -> list[Element]:
    """Keep interactive elements and anything with a name; drop off-screen; cap in tree order."""
    kept = [
        element
        for element in elements
        if not element.offscreen and (element.role in INTERACTIVE_ROLES or element.name)
    ]
    return kept[:cap]


def safe_name(name: str) -> str:
    """One printable line, capped: escapes control characters, truncates with an ellipsis."""
    escaped = "".join(
        ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii") for ch in name
    )
    if len(escaped) > MAX_NAME_CHARS:
        return escaped[: MAX_NAME_CHARS - 1] + "…"
    return escaped


def format_lines(elements: Sequence[Element], *, remaining: int = 0) -> list[str]:
    lines = []
    for element in elements:
        line = (
            f'{element.id} {"  " * element.depth}{element.role} "{safe_name(element.name)}" '
            f"[{element.box[0]},{element.box[1]},{element.box[2]},{element.box[3]}]"
        )
        if not element.enabled:
            line += " disabled"
        lines.append(line)
    if remaining > 0:
        lines.append(f"(... {remaining} more below)")
    return lines
