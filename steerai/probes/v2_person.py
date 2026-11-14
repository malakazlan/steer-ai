"""The suite's scripted person for v2 asks ([V2.15] Q11; packet v2-step4-claims §6).

A plain-field search may dispatch after a scripted yes only when the subgoal's commit is `none`,
the text is a goal span, and the field and the text are named in the request. "The field is
named" is read as: the field's name holds a whole word of the request, casefolded (packet §9).

Interface stub for step 4d. It lives with the suite, outside the v2 loop package (which no
other module may import), so it raises directly instead of joining the loop's stub registry; its
tests in `tests/loop2/test_invariants_person.py` are strict xfail until it is built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class AskFacts:
    """What the scripted person reads from one ask: closed facts, the field's name and the
    text."""

    subgoal_commit: Literal["none", "send", "submit", "delete", "pay"]
    text_from: Literal["request", "agent"] | None
    field_name: str
    text: str | None


def scripted_yes(goal: str, facts: AskFacts) -> bool:
    """True only when every Q11 condition holds."""
    raise NotImplementedError("step 4d: the scripted person")
