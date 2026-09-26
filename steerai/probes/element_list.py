"""Structured element list: the text representation a decision model sees (design 5.7, 6.2).

Each line: id, indentation by depth, role, quoted name, coarse box. Pruned to interactive elements
and named text or containers, off-screen elements dropped, capped with a remainder note.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from steerai.probes.uia_snapshot import RawNode, UiaClient

Box = tuple[int, int, int, int]

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
    automation_id: str = ""


def prune(elements: Sequence[Element], *, cap: int) -> list[Element]:
    """Keep interactive elements and anything with a name; drop off-screen; cap in tree order."""
    kept = [
        element
        for element in elements
        if not element.offscreen and (element.role in INTERACTIVE_ROLES or element.name)
    ]
    return kept[:cap]


def format_lines(elements: Sequence[Element], *, remaining: int = 0) -> list[str]:
    lines = [
        f'{element.id} {"  " * element.depth}{element.role} "{element.name}" '
        f"[{element.box[0]},{element.box[1]},{element.box[2]},{element.box[3]}]"
        for element in elements
    ]
    if remaining > 0:
        lines.append(f"(... {remaining} more below)")
    return lines


def elements_from_nodes(nodes: Sequence[RawNode]) -> list[Element]:
    return [
        Element(
            id=index + 1,
            depth=node.depth,
            role=node.role,
            name=node.name,
            box=node.box,
            offscreen=node.offscreen,
            automation_id=node.automation_id,
        )
        for index, node in enumerate(nodes)
    ]


def element_list_for_window(client: UiaClient, hwnd: int, *, cap: int = 150) -> list[str]:
    """One cached subtree fetch → pruned, capped, formatted lines."""
    tree = client.cached_tree(hwnd)
    elements = elements_from_nodes(tree.nodes)
    kept = prune(elements, cap=cap)
    eligible = prune(elements, cap=len(elements))
    return format_lines(kept, remaining=len(eligible) - len(kept))
