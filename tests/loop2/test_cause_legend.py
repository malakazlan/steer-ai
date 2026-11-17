"""[V2.85] (i), (ii), (iii), (iv): the wording of controller-15 that code cannot enforce. Every
cause token a refusal can show the model is named in the legend, every name there is a real
cause, each family says what the element does or what to do (never a control type), and no line
says to never repeat."""

from __future__ import annotations

import re

from steerai.loop2.controller import SYSTEM_RULES
from steerai.loop2.history import ACT_CAUSES, REFUSAL_TOKENS, UIA_CONTROL_TYPES

LINE = next(x for x in SYSTEM_RULES.split("\n") if x.startswith("- refused:<cause>"))
PATTERN = re.compile(r"no_\w+Pattern")
RUN_ENDING = {
    "takeover", "other_steer_input", "killed", "monitor_lost", "session_locked", "generation",
    "wall_budget",
}  # fmt: skip
"""A person's input, the kill switch or the budget ends the task; the model never sees these."""
NOT_REFUSALS = {
    "port_error", "readback_differs", "truncated", "settled_on_reread", "second_toggle_refused",
    "second_toggle_uncertain",
}  # fmt: skip
"""Details of a delivered or uncertain act, never of a refusal."""
TAUGHT_ELSEWHERE = ACT_CAUSES & REFUSAL_TOKENS
"""Causes that are refusal tokens of their own, each with its own rule or schema line."""
NAMED = set(re.findall(r"[a-z][A-Za-z_]+", LINE))


def test_every_cause_a_refusal_can_show_is_named_in_the_legend() -> None:
    shown = ACT_CAUSES - RUN_ENDING - NOT_REFUSALS - TAUGHT_ELSEWHERE
    missing = {t for t in shown if t not in NAMED and not PATTERN.fullmatch(t)}
    assert not missing, sorted(missing)
    assert "no_<name>Pattern" in LINE


def test_the_pattern_causes_are_written_once() -> None:
    assert not PATTERN.findall(LINE)


def test_every_name_in_the_legend_is_a_real_cause() -> None:
    tokens = {w for w in NAMED if "_" in w and w != "no_"}
    assert tokens <= ACT_CAUSES | {"not_delivered"}, sorted(tokens - ACT_CAUSES)


def test_each_family_says_what_the_element_does_or_what_to_do() -> None:
    for meaning in (
        "this element opens by click, not by expand",
        "another window is over it (temporary) or it has no area (it does not clear)",
        "minimized does not clear on its own",
        "offscreen, clipped: out of view; waiting does not help",
        "it does not offer that action; use another verb",
        "Temporary, try again after it clears",
    ):
        assert meaning in re.sub(r"\s+", " ", LINE), meaning


def test_the_legend_names_no_control_type_and_never_says_never_repeat() -> None:
    assert not set(re.findall(r"[A-Za-z]+", LINE)) & UIA_CONTROL_TYPES
    assert "never repeat" not in LINE.lower()


def test_the_meaning_and_param_lines_are_scoped_and_taught() -> None:
    meaning = next(x for x in SYSTEM_RULES.split("\n") if x.startswith("- delivered_verified"))
    assert "type, choose, check, uncheck, select, expand or collapse" in meaning
    assert "no read_back suffix" in meaning
    assert "code read the value or state back and it matches; claim the subgoal" in meaning
    assert "it only says the action was sent" in meaning
    assert "A param is the fewest words of the request that name the thing." in SYSTEM_RULES
