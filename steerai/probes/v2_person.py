"""The suite's scripted person for v2 asks ([V2.15] Q11, [V2.17] item 3; packet v2-step4-claims
§6).

A plain-field search may dispatch after a scripted yes only when all hold:
- the subgoal's commit is `none`;
- the text equals a goal span exactly;
- the field's name shares a content word with the request: not a stop word, at least
  `MIN_TARGET_CHARS` letters, casefolded whole tokens.
An unnamed field fails, and the run ends `user_declined`; the suite reports that count.

v1 already has the scripted person: `task_suite.Person`, which answers `yes:<id>` / `no:<id>`
and logs every ask, with `_named_in_goal` as its target rule (the whole target name, contiguous,
in the goal). 4d reuses that object and its minimum length, and plugs this rule in for v2 asks;
it adds no second person. v1's rule is unchanged.

Interface stub for step 4d. It lives with the suite, outside the v2 loop package (which no
other module may import), so it raises directly instead of joining the loop's stub registry; its
tests in `tests/loop2/test_invariants_person.py` are strict xfail until it is built.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from steerai.probes.task_suite import MIN_TARGET_CHARS

MIN_CONTENT_CHARS: Final = MIN_TARGET_CHARS
"""A content word holds at least this many letters (v1's target minimum)."""
STOP_WORDS: Final = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "onto",
        "this",
        "that",
        "these",
        "those",
        "your",
        "you",
        "our",
        "its",
        "are",
        "was",
        "not",
        "all",
        "any",
        "per",
        "via",
        "out",
        "off",
        "but",
        "has",
        "have",
        "then",
        "than",
    }
)
"""Words that never count as a shared content word."""


@dataclass(frozen=True)
class AskFacts:
    """What the scripted person reads from one ask: closed facts, the field's name and the
    text."""

    subgoal_commit: Literal["none", "send", "submit", "delete", "pay"]
    text_from: Literal["request", "agent"] | None
    field_name: str
    text: str | None


def shares_content_word(field_name: str, goal: str) -> bool:
    """True when a casefolded whole token of `field_name`, not in `STOP_WORDS` and at least
    `MIN_CONTENT_CHARS` letters long, is also a token of `goal`."""
    raise NotImplementedError("step 4d: the field-name rule")


def scripted_yes(goal: str, facts: AskFacts) -> bool:
    """True only when every condition above holds."""
    raise NotImplementedError("step 4d: the scripted person")


def unnamed_count(goal: str, asks: Sequence[AskFacts]) -> int:
    """How many asks were declined only because the field's name shares no content word with
    the request (reported per run, [V2.17] item 3)."""
    raise NotImplementedError("step 4d: the unnamed-field count")
