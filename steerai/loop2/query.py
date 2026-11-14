"""The query/commit classifier and the query outcomes (V2 §7.2, [V2.1], [V2.13], [V2.17] item 4;
probe #266).

The landmark facts come from `Observed.landmarks` ([V2.13]): each element's nearest landmark
ancestor. A missing map, a missing element or another landmark kind is never a search landmark,
so the query conditions fail closed and the action is a commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from steerai.loop2.task import Commit
from steerai.orchestrator.contracts import Risk
from steerai.world.effects import EffectReport, Observed, ValueFact, top_document

QueryOutcome = Literal["expected", "back_to_controller", "unexpected_effect"]
EnterToken = Literal["unexpected_effect", "unknown", "none", "no_carrier", "weak"]
FieldState = Literal["not_typed", "kept", "emptied", "gone", "unread"]
"""The typed field after the Enter or click: `not_typed` when no field typed under the subgoal
applies (row 2 is skipped), `gone` when it left the snapshot, `unread` when its value could not
be read."""
STRONG = frozenset({"navigated", "url_changed_in_place", "content_changed", "revealed"})
"""[V2.21] row 6's strong classes; a dialog beside them does not change the outcome."""
NEEDS_CARRIER = frozenset({"dialog_opened", "revealed", "content_changed"})
"""[V2.21] row 4: these without a carrier stop."""
WEAK_CLASSES = frozenset({"none", "focus", "own_state"})
WEAK_TOKENS = frozenset(
    {"frame_changed", "title_only", "dismissed", "rerendered", "moved", "scrolled"}
)
"""[V2.21] row 8: `rerendered` only at element level (row 3 takes a replaced top Document)."""


@dataclass(frozen=True)
class EnterOutcome:
    """[V2.21] #24 one row of the table: what happens next, its token and the row that matched."""

    outcome: QueryOutcome
    token: EnterToken
    row: int

    @property
    def releases_key(self) -> bool:
        """The caution key is released only on row 9 (proven `none`); rows 5, 7 and 8 keep it
        consumed, so a re-send needs a fresh ask."""
        return self.row == ROW_NONE


ROW_NONE = 9
_STOP: Final = "unexpected_effect"
_BACK: Final = "back_to_controller"


@dataclass(frozen=True)
class SearchFacts:
    """What the latest observation says about the field an Enter or a click acts for."""

    searchbox: bool
    """The field's AriaRole is `searchbox`."""
    in_search_landmark: bool
    """The field sits under a LandmarkType Search ancestor."""
    secure_in_form: bool
    payment_in_form: bool


def search_landmark_of(observed: Observed, element: tuple[int, ...]) -> tuple[int, ...] | None:
    """The RuntimeId of `element`'s nearest landmark when that landmark is a Search landmark;
    None when it is another kind, when it has none, or when the fact was not observed."""
    if observed.landmarks is None:
        return None
    landmark = observed.landmarks.get(element)
    if landmark is None or landmark.kind != "search":
        return None
    return landmark.runtime_id


def shares_search_landmark(
    observed: Observed, field: tuple[int, ...], button: tuple[int, ...]
) -> bool:
    """[V2.13] a button qualifies only when it sits in the field's own search landmark; any
    missing fact fails closed."""
    landmark = search_landmark_of(observed, field)
    return landmark is not None and search_landmark_of(observed, button) == landmark


def classify(  # noqa: PLR0913 - the closed facts of §7.2, keyword-only
    *,
    verb: Literal["press", "click"],
    field: SearchFacts,
    goal_span: bool,
    commit: Commit,
    deny_word: bool,
    same_landmark: bool = False,
) -> Risk:
    """R1 (a query) only when every §7.2 condition holds: a search field (searchbox or search
    landmark), goal-span text, commit `none`, no secure or payment field in the form, no deny
    word; a click counts only on a Button in the field's own search landmark. Otherwise R2."""
    if not goal_span or commit != "none" or deny_word:
        return "R2"
    if field.secure_in_form or field.payment_in_form:
        return "R2"
    if verb == "click":
        return "R1" if field.in_search_landmark and same_landmark else "R2"
    return "R1" if field.searchbox or field.in_search_landmark else "R2"


def field_state(before: ValueFact | None, after: ValueFact | None, *, present: bool) -> FieldState:
    """The typed field's state for row 2: `gone` when it is no longer in the snapshot, `unread`
    when either value was not read, `emptied` when it went from filled to empty, else `kept`."""
    if not present:
        return "gone"
    if before is None or after is None:
        return "unread"
    return "emptied" if before.filled and not after.filled else "kept"


def top_document_replaced(before: Observed, after: Observed) -> bool | None:
    """Row 3's fact: the top Document's RuntimeId changed; None when either has no top Document."""
    old, new = top_document(before.snapshot), top_document(after.snapshot)
    if old is None or new is None:
        return None
    return old.runtime_id != new.runtime_id


def query_outcome(  # noqa: PLR0911 - one return per row of the table
    report: EffectReport,
    *,
    text_carried: bool,
    typed_field: FieldState = "not_typed",
    top_replaced: bool | None = None,
) -> EnterOutcome:
    """[V2.21] #24 and [V2.22] #25: after a query or caution Enter, or a query or submit click,
    the first matching row wins. `text_carried` is the carrier (§7.1); `typed_field` the field
    typed under the subgoal (row 2); `top_replaced` whether the top Document was replaced
    (row 3). A fact the table needs and does not have is `unknown`, never a guess."""
    classes, tokens = report.classes, report.tokens
    if "windows_changed" in tokens:
        return EnterOutcome(_STOP, "unexpected_effect", 1)
    if typed_field == "emptied":
        return EnterOutcome(_STOP, "unexpected_effect", 2)
    if typed_field == "unread":
        return EnterOutcome(_BACK, "unknown", 2)
    if "rerendered" in tokens and top_replaced is not False:
        if top_replaced is None:
            return EnterOutcome(_BACK, "unknown", 3)
        return EnterOutcome(_STOP, "unexpected_effect", 3)
    if classes & NEEDS_CARRIER and not text_carried:
        return EnterOutcome(_STOP, "unexpected_effect", 4)
    if "unknown" in classes or (not classes and report.unread):
        return EnterOutcome(_BACK, "unknown", 5)
    if classes & STRONG and text_carried:
        return EnterOutcome("expected", "none", 6)
    if "navigated" in classes:
        return EnterOutcome(_BACK, "no_carrier", 7)
    if _weak_only(report, typed_field):
        return EnterOutcome(_BACK, "weak", 8)
    if classes == frozenset({"none"}) and not tokens and not report.unread:
        return EnterOutcome(_BACK, "none", ROW_NONE)
    return EnterOutcome(_BACK, "unknown", 5)


def _weak_only(report: EffectReport, typed_field: FieldState) -> bool:
    """Row 8: only weak signals, and at least one: `own_value` counts while the field is still
    filled (`kept`); nothing unread."""
    weak = WEAK_CLASSES | ({"own_value"} if typed_field == "kept" else set())
    seen = (report.classes - {"none"}) | report.tokens
    return (
        bool(seen) and report.classes <= weak and report.tokens <= WEAK_TOKENS and not report.unread
    )
