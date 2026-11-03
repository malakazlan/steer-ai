"""Screen fingerprints (design 5.7 [v0.2], [v1.0]): stable landmarks compared by similarity.

A fingerprint is the process name, a normalised window title and the set of stable landmarks.
Dynamic content (list rows, data items, text, tab titles, link names) is excluded so a live page
produces the same fingerprint as its content changes; trailing counts such as "Inbox (3)" are
stripped. The title is itself a landmark, so two dialogs that share buttons but differ in title do
not match (WM-04). Two screens match when their processes are known and equal, the landmark sets
clear a minimum size, and their Jaccard similarity clears the threshold; equality is never used.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from steerai.world.elements import Element

Landmark = tuple[str, str]

MIN_MARKS = 4
_DYNAMIC_ROLES = frozenset(
    {"ListItem", "DataItem", "Text", "TreeItem", "Image", "Window", "TabItem", "Hyperlink"}
)
_COUNT_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")
_LEADING_COUNT = re.compile(r"^\s*\(\d+\)\s*")
_DIGITS = re.compile(r"\d+")


def normalise_title(title: str) -> str:
    """Window title with counters and numbers removed, so "(29) Inbox - App" ≈ "Inbox - App"."""
    stripped = _LEADING_COUNT.sub("", _COUNT_SUFFIX.sub("", title))
    return _DIGITS.sub("#", stripped).strip().casefold()


def landmarks(elements: Sequence[Element], *, title: str = "") -> frozenset[Landmark]:
    marks: set[Landmark] = set()
    if title:
        marks.add(("title", normalise_title(title)))
    for element in elements:
        if element.role in _DYNAMIC_ROLES:
            continue
        if element.automation_id:
            marks.add((element.role, "#" + element.automation_id))
        elif element.name:
            marks.add((element.role, _COUNT_SUFFIX.sub("", element.name)))
    return frozenset(marks)


def similarity(a: frozenset[Landmark], b: frozenset[Landmark]) -> float:
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


@dataclass(frozen=True, slots=True)
class Fingerprint:
    process: str
    title_pattern: str
    marks: frozenset[Landmark]

    def matches(self, other: Fingerprint, *, threshold: float) -> bool:
        """Known equal process, enough landmarks on both sides, similarity at or above threshold."""
        if not self.process or not other.process:
            return False
        if self.process.casefold() != other.process.casefold():
            return False
        if len(self.marks) < MIN_MARKS or len(other.marks) < MIN_MARKS:
            return False
        return similarity(self.marks, other.marks) >= threshold
