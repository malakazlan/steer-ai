"""The predicate language (design 5.10 [v0.2]): the only vocabulary for expected effects and
checklist conditions, app-agnostic and over the element list.

Patterns are plain text, never regular expressions: a model cannot smuggle a wildcard or a
control character into a check. They are normalised once (NFKC, collapsed whitespace); a
window-wide match is anchored and full-string, a scoped match is a case-insensitive substring.
Patterns and values are kept out of `repr` because labels and traces carry predicates (SX-28).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from steerai.world.model import normalise_name

StateName = Literal["on", "off", "selected"]
_STATES: frozenset[str] = frozenset({"on", "off", "selected"})


def _clean_pattern(text: str, what: str) -> str:
    normalised = normalise_name(text)
    if not normalised:
        raise ValueError(f"{what} pattern is empty")
    for ch in normalised:
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            raise ValueError(f"{what} pattern holds a control or invisible character")
    return normalised


def _comparable(text: str) -> str:
    # Format characters (ZWSP, ZWJ, bidi marks) are invisible decoration, never content (VF-24).
    return "".join(ch for ch in normalise_name(text) if unicodedata.category(ch) != "Cf")


def name_matches(name: str, pattern: str, *, scoped: bool) -> bool:
    """Always full-string (a substring hit on a longer name is another author's message, a
    quoted reply or a status row, VF-06); scoped matches ignore case, window-wide ones do not.
    Patterns are plain text: no regular expression is ever compiled from them."""
    normalised = _comparable(name)
    wanted = _comparable(pattern)
    if scoped:
        return normalised.casefold() == wanted.casefold()
    return normalised == wanted


@dataclass(frozen=True, slots=True)
class Within:
    """A region: a named landmark container, or the nearest container of an anchor element."""

    landmark: str | None = None
    anchor_id: int | None = None

    def __post_init__(self) -> None:
        if (self.landmark is None) == (self.anchor_id is None):
            raise ValueError("Within takes a landmark name or an anchor id, not both or neither")
        if self.landmark is not None:
            object.__setattr__(self, "landmark", normalise_name(self.landmark).casefold())
            if not self.landmark:
                raise ValueError("Within landmark is empty")


@dataclass(frozen=True, slots=True)
class Appeared:
    """Present after and absent before: only an element with a new RuntimeId counts."""

    role: str
    name_pattern: str = field(repr=False)
    within: Within | None = None
    excluding_anchor: int | None = None
    """Subtree to ignore (the step's own composer in send templates, 5.10 [v0.4])."""
    expected: int = 1
    """How many matching elements must appear: more is a duplicate effect (VF-13)."""
    order: Literal["append", "prepend"] = "append"
    """Where the list grows: a conversation appends, a Sent folder prepends."""

    def __post_init__(self) -> None:
        _check_role(self.role)
        object.__setattr__(self, "name_pattern", _clean_pattern(self.name_pattern, "name"))
        if self.expected <= 0:
            raise ValueError("expected must be > 0")
        if self.order not in ("append", "prepend"):
            raise ValueError("order must be append or prepend")


@dataclass(frozen=True, slots=True)
class Disappeared:
    role: str
    name_pattern: str = field(repr=False)
    within: Within | None = None

    def __post_init__(self) -> None:
        _check_role(self.role)
        object.__setattr__(self, "name_pattern", _clean_pattern(self.name_pattern, "name"))


@dataclass(frozen=True, slots=True)
class ValueEquals:
    target_id: int
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Focused:
    """Never a valid postcondition on its own (5.10 [v0.3])."""

    target_id: int


@dataclass(frozen=True, slots=True)
class State:
    target_id: int
    wanted: StateName

    def __post_init__(self) -> None:
        if self.wanted not in _STATES:
            raise ValueError(f"state must be one of {sorted(_STATES)}")


@dataclass(frozen=True, slots=True)
class TitleMatches:
    pattern: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "pattern", _clean_pattern(self.pattern, "title"))


@dataclass(frozen=True, slots=True)
class WindowExists:
    name_pattern: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name_pattern", _clean_pattern(self.name_pattern, "window"))


@dataclass(frozen=True, slots=True)
class WindowGone:
    """A top-level window present before the step and absent after (compose window closed)."""

    name_pattern: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name_pattern", _clean_pattern(self.name_pattern, "window"))


@dataclass(frozen=True, slots=True)
class CountDelta:
    role: str
    delta: int
    within: Within | None = None

    def __post_init__(self) -> None:
        _check_role(self.role)
        if self.delta == 0:
            raise ValueError("delta must be non-zero")


@dataclass(frozen=True, slots=True)
class All:
    parts: tuple[Predicate, ...]

    def __post_init__(self) -> None:
        if len(self.parts) < 2:
            raise ValueError("All needs at least two parts")


@dataclass(frozen=True, slots=True)
class AnyOf:
    parts: tuple[Predicate, ...]

    def __post_init__(self) -> None:
        if len(self.parts) < 2:
            raise ValueError("AnyOf needs at least two parts")


Predicate = (
    Appeared
    | Disappeared
    | ValueEquals
    | Focused
    | State
    | TitleMatches
    | WindowExists
    | WindowGone
    | CountDelta
    | All
    | AnyOf
)


def _check_role(role: str) -> None:
    if not role.strip():
        raise ValueError("role must be named")
