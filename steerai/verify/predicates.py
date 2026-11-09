"""The predicate language (design 5.10 [v0.2]): the only vocabulary for expected effects and
checklist conditions, app-agnostic and over the element list.

Patterns are plain text, never regular expressions: a model cannot smuggle a wildcard or a
control character into a check. They are normalised once (NFKC, collapsed whitespace); a
window-wide match is anchored and full-string, a scoped match is a case-insensitive substring.
Patterns and values are kept out of `repr` because labels and traces carry predicates (SX-28).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, cast

from steerai.verify.decorations import decorate
from steerai.world.model import normalise_name

StateName = Literal["on", "off", "selected"]
_STATES: frozenset[str] = frozenset({"on", "off", "selected"})
STATE_ROLES: Mapping[StateName, frozenset[str]] = {
    "on": frozenset({"CheckBox"}),
    "off": frozenset({"CheckBox"}),
    "selected": frozenset({"TabItem", "RadioButton"}),
}
"""The roles each wanted state is read on (W3 verdict ruling (c)): a CheckBox's ToggleState is
on or off; a TabItem's or a RadioButton's SelectionItem is selected. Role and state agree."""


def _clean_pattern(text: str, what: str) -> str:
    normalised = normalise_name(text)
    if not normalised:
        raise ValueError(f"{what} pattern is empty")
    for ch in normalised:
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            raise ValueError(f"{what} pattern holds a control or invisible character")
    return normalised


def comparable(text: str) -> str:
    """The one comparable form of screen text: NFKC, format characters removed (ZWSP, ZWJ, bidi
    marks are invisible decoration, never content, VF-24), case-folded."""
    return _comparable(text).casefold()


def _comparable(text: str) -> str:
    # case-preserving: window-wide name matches do not ignore case (VF-06)
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


REPLACING_ROLES: frozenset[str] = frozenset({"Document"})
"""Roles whose `appeared` may take the replacing form (5.10 [v1.7]): a page replaces a page; a
list row never replaces, so the vanish and order rules stay in force for every other role."""


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
    decoration: str | None = None
    """A code-owned frame around the content (VF-34): the gate judges `name_pattern`, the
    verifier matches `name_match`."""
    replaces: bool = False
    """The navigation form: exactly one element of the role in scope vanishes as the new one
    appears (a page's Document replaced by the next page's). A list never replaces."""

    def __post_init__(self) -> None:
        _check_role(self.role)
        object.__setattr__(self, "name_pattern", _clean_pattern(self.name_pattern, "name"))
        if self.expected <= 0:
            raise ValueError("expected must be > 0")
        if self.order not in ("append", "prepend"):
            raise ValueError("order must be append or prepend")
        if self.decoration is not None:
            decorate(self.decoration, self.name_pattern)  # refuses an unknown key
        if self.replaces:
            if self.role not in REPLACING_ROLES:
                raise ValueError(f"replaces is only for {sorted(REPLACING_ROLES)}, not {self.role}")
            if self.expected != 1 or self.order != "append" or self.within is not None:
                raise ValueError(
                    "replaces is the single, window-wide form: no expected, order or within"
                )

    @property
    def name_match(self) -> str:
        """The full name to match on screen: the pattern inside its decoration, if any."""
        if self.decoration is None:
            return self.name_pattern
        return decorate(self.decoration, self.name_pattern)


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
    folded: bool = False
    """A choose's template (W4-05): compared by `normalise_name` (NFKC folds a no-break space,
    whitespace folded, case kept). A type's stays exact: newlines normalised, nothing else."""


FIELD_ROLES: frozenset[str] = frozenset({"Edit", "ComboBox", "Document"})
"""Roles whose value is what was typed (= `steerai.action.physical.TEXT_ROLES`, pinned by a
test): on any other role the read is the element's own name (field-value gate item 3)."""
MAX_FIELD_TEXT_UNITS = 500
"""The longest text a `type` step sends, in UTF-16 units (section 9): a longer condition can
never be reached (hunter FV-38)."""


@dataclass(frozen=True, slots=True)
class FieldValue:
    """A subgoal condition only (issue #36): the one field of this role and name in the
    judged snapshot holds exactly this text (the `ValueEquals` rule: newlines normalised,
    case-sensitive). Never a step `expect`: presence has no before value (VF-30 B1)."""

    role: str
    name_pattern: str = field(repr=False)
    text: str = field(repr=False)
    folded: bool = False
    """Set by the plan boundary, never by the model, when a `choose` of the subgoal proves this
    value (W4-7): the choose's comparator (`normalise_name`) and no alias to another field role
    (W4-05, W4-10). A value a `type` proves keeps the exact rule."""

    def __post_init__(self) -> None:
        if self.role not in FIELD_ROLES:
            raise ValueError(f"value role must be one of {sorted(FIELD_ROLES)}")
        if not normalise_name(self.name_pattern):
            raise ValueError("value name must not be empty")
        if not self.text.strip():
            raise ValueError("value text must not be empty: an unreadable field reads empty")
        if len(self.text.encode("utf-16-le")) // 2 > MAX_FIELD_TEXT_UNITS:
            raise ValueError(f"value text is over {MAX_FIELD_TEXT_UNITS} UTF-16 units")


def state_problem(role: str, wanted: object) -> tuple[str, str] | None:
    """(field, reason) when a `state` role and wanted value do not agree (W3 verdict ruling
    (c)), else None: shared by `CheckState` and the plan boundary, which names the field."""
    if wanted not in _STATES:
        return "wanted", "state wanted must be on, off or selected"
    if not any(role in roles for roles in STATE_ROLES.values()):
        known = sorted({r for roles in STATE_ROLES.values() for r in roles})
        return "role", f"state role must be one of {known}"
    if role not in STATE_ROLES[cast("StateName", wanted)]:
        allowed = " or ".join(w for w in ("on", "off", "selected") if role in STATE_ROLES[w])
        return "wanted", f"state wanted for a {role} must be {allowed}"
    return None


@dataclass(frozen=True, slots=True)
class CheckState:
    """A subgoal condition only (W2-4, W3-5): the one element of this role and name (NFKC,
    whitespace-folded, case-folded, exactly one) in the judged snapshot reads the wanted state:
    a CheckBox on or off, a TabItem or a RadioButton selected. Never a step `expect`: a check,
    uncheck or select verb owns its own postcondition (its `State` template)."""

    role: str
    name_pattern: str = field(repr=False)
    wanted: StateName

    def __post_init__(self) -> None:
        problem = state_problem(self.role, self.wanted)
        if problem is not None:
            raise ValueError(problem[1])
        if not normalise_name(self.name_pattern):
            raise ValueError("state name must not be empty")


@dataclass(frozen=True, slots=True)
class Present:
    """A state (#124, 5.10 [v2.36]): exactly one visible element of this role and full name in
    the landmark, judged on one snapshot. Only the proof of an R0/R1 click that selects what may
    already be selected (a chat already open), and that click's one-step subgoal; never a start
    tick, never inside `all`/`any_of`, never an effect for R2/R3 (hunter PS-01..07)."""

    role: str
    name_pattern: str = field(repr=False)
    within: Within
    target_id: int | None = None
    """The clicked element in the before snapshot, bound by the gate: the subtree of its nearest
    container never counts, so the clicked row's own text never proves the selection."""
    excluding: tuple[str, ...] = ()
    """The plan's content landmarks, bound by the loop: a message there reading exactly the
    name never proves the chat (hunter PS-09); each must resolve to one container."""

    def __post_init__(self) -> None:
        _check_role(self.role)
        object.__setattr__(self, "name_pattern", _clean_pattern(self.name_pattern, "name"))
        if self.within.landmark is None:
            raise ValueError("present takes a landmark, never an anchor")
        names = tuple(normalise_name(name).casefold() for name in self.excluding)
        if not all(names):
            raise ValueError("an excluding landmark is empty")
        object.__setattr__(self, "excluding", names)


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
    | FieldValue
    | CheckState
    | Present
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


def presents(predicate: Predicate) -> list[Present]:
    """Every `present` part of a predicate, at any depth."""
    if isinstance(predicate, Present):
        return [predicate]
    if isinstance(predicate, All | AnyOf):
        return [found for part in predicate.parts for found in presents(part)]
    return []


def field_values(predicate: Predicate) -> list[FieldValue]:
    """Every `value` part of a condition, at any depth."""
    if isinstance(predicate, FieldValue):
        return [predicate]
    if isinstance(predicate, All | AnyOf):
        return [found for part in predicate.parts for found in field_values(part)]
    return []


def check_states(predicate: Predicate) -> list[CheckState]:
    """Every `state` part of a condition, at any depth."""
    if isinstance(predicate, CheckState):
        return [predicate]
    if isinstance(predicate, All | AnyOf):
        return [found for part in predicate.parts for found in check_states(part)]
    return []
