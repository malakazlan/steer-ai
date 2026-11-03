"""Screen fingerprints (design 5.7 [v0.2]): a set of stable landmarks compared by similarity.

Dynamic content (list rows, data items, plain text, window titles) is excluded so a live page
produces the same fingerprint as its content changes. Two screens match when the Jaccard
similarity of their landmark sets clears a threshold; equality is never used.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from steerai.world.elements import Element

Landmark = tuple[str, str]

_DYNAMIC_ROLES = frozenset({"ListItem", "DataItem", "Text", "TreeItem", "Image", "Window"})


def landmarks(elements: Sequence[Element]) -> frozenset[Landmark]:
    marks: set[Landmark] = set()
    for element in elements:
        if element.role in _DYNAMIC_ROLES:
            continue
        if element.automation_id:
            marks.add((element.role, "#" + element.automation_id))
        elif element.name:
            marks.add((element.role, element.name))
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
        if self.process.lower() != other.process.lower():
            return False
        return similarity(self.marks, other.marks) >= threshold
