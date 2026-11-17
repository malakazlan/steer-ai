"""[V2.85] (i), (ii), (iii), (iv): the wording of controller-15 that code cannot enforce. Every
cause token the legend names is a real cause; each line says what the element does or what to do,
never a control type, and no line says to never repeat."""

from __future__ import annotations

import re

from steerai.loop2.controller import SYSTEM_RULES
from steerai.loop2.history import ACT_CAUSES, UIA_CONTROL_TYPES

LINE = next(line for line in SYSTEM_RULES.split("\n") if line.startswith("- refused:<cause>"))
NAMED = set(re.findall(r"\b[a-z][a-zA-Z_]+(?=[:,])", LINE.split("says why", 1)[1])) - {
    "temporary",
    "use",
}
MEANT = {
    "expand_needs_click": "opens by click, not by expand",
    "covered": "temporary",
    "target_moved": "temporary",
    "target_gone": "read the listing again",
}


def test_every_cause_the_legend_names_is_a_real_cause() -> None:
    named = {t for t in NAMED if "_" in t or t in ACT_CAUSES}
    assert {"expand_needs_click", "covered", "target_moved", "not_delivered"} <= named
    assert named - {"not_delivered"} <= ACT_CAUSES, sorted(named - ACT_CAUSES)


def test_each_cause_line_says_what_the_element_does_or_what_to_do() -> None:
    for token, meaning in MEANT.items():
        assert token in LINE
        assert meaning in LINE, token


def test_the_legend_names_no_control_type_and_never_says_never_repeat() -> None:
    words = set(re.findall(r"[A-Za-z]+", LINE))
    assert not words & UIA_CONTROL_TYPES
    assert "never repeat" not in LINE.lower()


def test_the_meaning_and_param_lines_are_taught() -> None:
    assert "code read the state or value back and it matches; claim the subgoal" in SYSTEM_RULES
    assert "A param is the fewest words of the request that name the thing." in SYSTEM_RULES
