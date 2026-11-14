"""The query/commit classifier and the query outcomes (V2 §7.2, [V2.1], [V2.13], [V2.17] item 4;
probe #266).

The landmark facts come from `Observed.landmarks` ([V2.13]): each element's nearest landmark
ancestor. A missing map, a missing element or another landmark kind is never a search landmark,
so the query conditions fail closed and the action is a commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from steerai.loop2.task import Commit
from steerai.orchestrator.contracts import Risk
from steerai.world.effects import EffectReport, Observed

QueryOutcome = Literal["expected", "back_to_controller", "unexpected_effect"]
IN_PLACE = frozenset({"url_changed_in_place", "content_changed"})
WEAK_CLASSES = frozenset({"none", "focus", "own_state"})
"""[V2.17] item 4: weak only (with `none`) goes back to the controller as none-like."""
WEAK_TOKENS = frozenset({"moved", "frame_changed"})
LEFT_THE_SCOPE = frozenset({"dialog_opened"})
LEFT_THE_SCOPE_TOKENS = frozenset({"windows_changed"})


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


def query_outcome(report: EffectReport, *, text_carried: bool) -> QueryOutcome:
    """After a query (or caution) Enter or click ([V2.1], [V2.15] Q2, [V2.17] item 4):

    - a navigation, or an in-place change carrying the text: expected;
    - `none`, weak effects only (`focus`, `own_state`, `frame_changed`), `unknown` or an unread
      fact alone: back to the controller (a re-send needs a fresh ask);
    - anything else (a dialog or a window opened, content without the text, a revealed list):
      `unexpected_effect`.
    """
    if report.classes & LEFT_THE_SCOPE or report.tokens & LEFT_THE_SCOPE_TOKENS:
        return "unexpected_effect"
    if "navigated" in report.classes:
        return "expected"
    if report.classes & IN_PLACE:
        return "expected" if text_carried else "unexpected_effect"
    if "unknown" in report.classes:
        return "back_to_controller"
    if report.classes <= WEAK_CLASSES and report.tokens <= WEAK_TOKENS:
        return "back_to_controller"
    return "unexpected_effect"
