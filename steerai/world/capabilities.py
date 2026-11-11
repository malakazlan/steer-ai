"""What an element can do, as closed tokens (packet interaction-model-2026-11-12 §1).

The platform defines the space: Microsoft's table of control types and the control patterns each
must or may support, and the W3C Core-AAM 1.2 mapping of every HTML/ARIA role to a UIA control type
and patterns, which Chromium implements. A switch is a Button with Toggle, a menuitemcheckbox a
MenuItem with Toggle, an option a ListItem with SelectionItem, a menu button a Button with
ExpandCollapse. The role alone does not say which verb applies; the patterns do. The snapshot reads
the pattern-availability and pattern-state properties in its one cached fetch, and the listing
shows them as these tokens, in this fixed order, after the box and the flags.
"""

from __future__ import annotations

from dataclasses import dataclass

TOGGLE_STATES: dict[int, str] = {0: "off", 1: "on", 2: "mixed"}
"""UIA ToggleState: Off, On, Indeterminate."""

EXPANDED_STATES: frozenset[int] = frozenset({1, 2})
"""ExpandCollapseState Expanded and PartiallyExpanded read as `expanded` in the listing; the
`expand` verb's own readback still demands 1 (W5). LeafNode (3) has nothing to open or close."""

TOKENS: tuple[str, ...] = (
    "toggle:on",
    "toggle:off",
    "toggle:mixed",
    "selected",
    "selectable",
    "expanded",
    "collapsed",
    "editable",
    "readonly",
    "range",
)
"""The closed vocabulary; a listing line never carries anything else after the flags."""


@dataclass(frozen=True, slots=True)
class Caps:
    """The pattern facts of one element; None means the pattern is absent."""

    toggle: int | None = None
    """ToggleState when the Toggle pattern is available."""
    selected: bool | None = None
    """SelectionItem.IsSelected when the SelectionItem pattern is available."""
    expanded: int | None = None
    """ExpandCollapseState when the ExpandCollapse pattern is available."""
    read_only: bool | None = None
    """Value.IsReadOnly when the Value pattern is available (never the value itself, WM-23)."""
    range_value: bool = False


def tokens(caps: Caps) -> tuple[str, ...]:
    """The tokens in the fixed order: toggle, selection, expand, value, range. Invoke has no token:
    it never changes the verb (principal, packet interaction-model-2026-11-12)."""
    out: list[str] = []
    if caps.toggle is not None and caps.toggle in TOGGLE_STATES:
        out.append(f"toggle:{TOGGLE_STATES[caps.toggle]}")
    if caps.selected is not None:
        out.append("selected" if caps.selected else "selectable")
    if caps.expanded is not None:
        if caps.expanded in EXPANDED_STATES:
            out.append("expanded")
        elif caps.expanded == 0:
            out.append("collapsed")
    if caps.read_only is not None:
        out.append("readonly" if caps.read_only else "editable")
    if caps.range_value:
        out.append("range")
    return tuple(out)
