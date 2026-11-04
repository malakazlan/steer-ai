"""Person targets in the loop (design 5.8 [v1.5]): the candidates a parameter names, the lines
a person is asked to choose from, and the binding that carries the choice through the step.

The binding is (role, RuntimeId, comparable name part, full name) in the step's running state
and in the write-ahead record; it is never a positional id and never in a plan. After every
re-observe the chosen element is found again by RuntimeId with a role and name-part re-check,
else by a unique full-name match, else it is gone (`candidate_gone`). Nothing here carries a
name into a repr: the console shows names, the trace never does.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from steerai.orchestrator.contracts import Target
from steerai.world.elements import Element
from steerai.world.model import Snapshot, normalise_name
from steerai.world.people import (
    MAX_CANDIDATES,
    candidates,
    comparable,
    lookalike_only,
    matches,
    mixed_script,
    name_part,
    preview,
)


@dataclass(frozen=True, slots=True)
class Binding:
    role: str
    runtime_id: tuple[int, ...] = field(repr=False)
    name_part: str = field(repr=False)
    """The comparable name part (NFKC, case-folded, confusables folded)."""
    full_name: str = field(repr=False)
    unique_name: bool = True
    """Whether the full name was unique among the ask-time candidates; only then may a
    re-rendered row be found again by its name (PT-08, review item 3)."""

    def to_record(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "runtime_id": list(self.runtime_id),
            "name_part": self.name_part,
            "full_name": self.full_name,
            "unique_name": self.unique_name,
        }

    @classmethod
    def from_record(cls, payload: Any) -> Binding | None:
        if not isinstance(payload, dict) or not isinstance(payload.get("runtime_id"), list | tuple):
            return None
        try:
            return cls(
                role=str(payload["role"]),
                runtime_id=tuple(int(v) for v in payload["runtime_id"]),
                name_part=str(payload["name_part"]),
                full_name=str(payload["full_name"]),
                unique_name=bool(payload.get("unique_name", True)),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True, slots=True)
class CandidateLine:
    """One choice as the console shows it; names stay out of the repr."""

    index: int
    role: str
    name_part: str = field(repr=False)
    preview: str = field(repr=False)
    tags: tuple[str, ...] = ()


def pick(snapshot: Snapshot, target: Target) -> list[Element]:
    return list(candidates(snapshot.elements, target.role, target.name))


def other_role_matches(snapshot: Snapshot, target: Target) -> int:
    return sum(
        1 for e in snapshot.elements if e.role != target.role and matches(target.name, e.name)
    )


def bind(element: Element, among: Sequence[Element] = ()) -> Binding:
    """The binding for a chosen element; `among` are the other candidates at ask time."""
    wanted = normalise_name(element.name).casefold()
    twins = [
        other
        for other in among
        if tuple(other.runtime_id) != tuple(element.runtime_id)
        and normalise_name(other.name).casefold() == wanted
    ]
    return Binding(
        role=element.role,
        runtime_id=tuple(element.runtime_id),
        name_part=comparable(name_part(element.name)),
        full_name=element.name,
        unique_name=not twins,
    )


def lines_for(elements: Sequence[Element], parameter: str) -> list[CandidateLine]:
    """Numbered 1..k in document order with the tags a person needs to tell them apart
    (PT-13/32): a lookalike-only match, a mixed-script name part, two name parts that agree
    only after folding, an off-screen or disabled row."""
    parts = [comparable(name_part(e.name)) for e in elements]
    lines: list[CandidateLine] = []
    for index, element in enumerate(elements, start=1):
        tags: list[str] = []
        if lookalike_only(parameter, element.name):
            tags.append("lookalike")
        if mixed_script(element.name):
            tags.append("mixed script")
        twins = [
            j + 1
            for j, other in enumerate(parts)
            if j != index - 1
            and other == parts[index - 1]
            and normalise_name(name_part(elements[j].name)).casefold()
            != normalise_name(name_part(element.name)).casefold()
        ]
        if twins:
            tags.append(f"lookalike of #{twins[0]}")
        if element.offscreen:
            tags.append("off screen")
        if not element.enabled:
            tags.append("disabled")
        lines.append(
            CandidateLine(
                index=index,
                role=element.role,
                name_part=name_part(element.name),
                preview=preview(element.name),
                tags=tuple(tags),
            )
        )
    return lines


def rebind(binding: Binding, snapshot: Snapshot) -> Element | None:
    """The bound element in a fresh snapshot: by RuntimeId with a role and name-part re-check
    (PT-08), else the one element of the role with the same full name, else None."""
    by_runtime = [
        e
        for e in snapshot.elements
        if tuple(e.runtime_id) == binding.runtime_id
        and e.role == binding.role
        and comparable(name_part(e.name)) == binding.name_part
    ]
    if len(by_runtime) == 1:
        return by_runtime[0]
    if not binding.unique_name:
        return None  # a twin by name existed at ask time: the name proves nothing
    wanted = normalise_name(binding.full_name).casefold()
    by_name = [
        e
        for e in snapshot.elements
        if e.role == binding.role and normalise_name(e.name).casefold() == wanted
    ]
    return by_name[0] if len(by_name) == 1 else None


def too_many(elements: Iterable[Element]) -> bool:
    return sum(1 for _ in elements) > MAX_CANDIDATES
