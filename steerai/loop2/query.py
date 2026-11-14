"""The query/commit classifier and the query outcomes (V2 §7.2, [V2.1]; probe #266).

Interface stub for step 3c; the tests in `tests/loop2/test_invariants_query.py` come first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from steerai.loop2._stub import stub
from steerai.loop2.task import Commit
from steerai.orchestrator.contracts import Risk
from steerai.world.effects import EffectReport

QueryOutcome = Literal["expected", "back_to_controller", "unexpected_effect"]


@dataclass(frozen=True)
class SearchFacts:
    """What the latest observation says about the field an Enter or a click acts for."""

    searchbox: bool
    """The field's AriaRole is `searchbox`."""
    in_search_landmark: bool
    """The field sits under a LandmarkType Search ancestor."""
    secure_in_form: bool
    payment_in_form: bool


@stub
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
    raise NotImplementedError("3c: query classifier")


@stub
def query_outcome(report: EffectReport, *, text_carried: bool) -> QueryOutcome:
    """After a query Enter or click: a navigation or an in-place change carrying the text is
    expected; `none` goes back to the controller; anything else is `unexpected_effect`."""
    raise NotImplementedError("3c: query outcomes")
