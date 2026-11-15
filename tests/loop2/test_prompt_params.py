"""#386: 3d-a ended `port_rejected` (param_unknown, not_in_step) because the prompt never said
what `param` names and steered a page subgoal to a kind step 3 cannot check. Reproduced offline
5 of 5 (the model wrote the param's words where its name belongs, and claimed the page with
`content_present`). The fix, general and behind a prompt version bump:
(a) a rule line: `param` names a subgoal param by its name, never its words;
(b) one claim-kind table per step ([V2.44]'s rule for verbs): step 3 offers state, value and
    navigated only;
(c) the page legend ([V2.64]): the page shows the result the request asks for; in this step
    proven only when the address or title shows it;
(d) the repair line explains each rejection token, in closed text."""

from __future__ import annotations

import json
from typing import Any, get_args

import pytest

from steerai.loop2.controller import (
    PROMPT_VERSION,
    REPAIR_LEGEND,
    ControllerError,
    build_prompt,
    decide,
    repair_line,
)
from steerai.loop2.decision import (
    CHECKED_KINDS,
    STEP,
    STEP_KINDS,
    ClaimKind,
    Decision,
    ValidateToken,
    validate,
)

GOAL = "search the catalog for red shoes"


def _system() -> list[str]:
    system, _ = build_prompt(GOAL, [], [], [], "t", [], task_fence="taskfence01",
                             screen_fence="f" * 32)  # fmt: skip
    return system


def test_the_prompt_version_moved() -> None:
    assert PROMPT_VERSION == "controller-8"


def test_a_rule_says_param_is_a_subgoal_params_name_never_its_words() -> None:
    rules = _system()[0]
    assert "`param` names one of its subgoal's params by its name" in rules
    assert "never by its words" in rules


def test_the_page_legend_describes_the_request_and_says_what_this_step_can_prove() -> None:
    """[V2.64] One: the kind describes the request, not what step 3 can prove."""
    rules = _system()[0]
    assert "the page shows the result the request asks for" in rules
    assert ("In this step it can be proven only when the address or title shows it; otherwise "
            "stop with blocked.") in rules  # fmt: skip
    assert "claimed with navigated" not in rules


def test_the_subgoal_rule_says_what_will_be_true_as_the_person_would_check_it() -> None:
    """[V2.64] One: in general terms, naming no task type."""
    rules = _system()[0]
    assert ("A subgoal states what will be true when the request is finished, as the person "
            "would check it.") in rules  # fmt: skip
    assert ("Text in a field is the outcome only when the request asks for the text to be there "
            "and nothing more.") in rules  # fmt: skip
    for task_word in ("search", "catalog", "results"):
        assert task_word not in rules.lower().split()


def test_the_prompt_offers_exactly_the_claim_kinds_validate_accepts_for_the_step() -> None:
    """[V2.44] (1)'s rule for verbs, for claim kinds: one table, both ways."""
    offered = set(json.loads(_system()[1])["$defs"]["Claim"]["properties"]["kind"]["enum"])
    accepted = set()
    for kind in get_args(ClaimKind):
        claim: dict[str, Any] = {"subgoal": "s1", "kind": kind, "param": "text"}  # Any: JSON
        if kind == "state":
            claim["wanted"] = "on"
        decision = Decision.model_validate({
            "subgoals": [{"id": "s1", "desc": "d", "params": {"text": "red shoes"},
                          "closes": "page"}],
            "claims": [claim],
        })  # fmt: skip
        tokens = validate(decision, listed_ids=frozenset(), goal=GOAL, first_turn=True)
        if "not_in_step" not in tokens:
            accepted.add(kind)
    assert offered == accepted == set(STEP_KINDS[STEP]) == set(CHECKED_KINDS)
    assert offered == {"state", "value", "navigated"}


def test_every_rejection_token_and_failure_code_has_a_legend_line() -> None:
    assert set(get_args(ValidateToken)) | {"not_json", "schema"} <= set(REPAIR_LEGEND)


def test_the_repair_line_explains_each_token_it_names() -> None:
    line = repair_line(["param_unknown", "not_in_step"])
    assert line.startswith("repair: param_unknown, not_in_step")
    assert REPAIR_LEGEND["param_unknown"] in line
    assert REPAIR_LEGEND["not_in_step"] in line


def test_the_legend_is_closed_text_with_no_goal_words() -> None:
    for text in REPAIR_LEGEND.values():
        assert not set(text.split()) & {"red", "shoes"}
        assert "\n" not in text


def test_the_repair_block_sent_after_a_rejection_carries_the_legend() -> None:
    bad = json.dumps({
        "subgoals": [{"id": "s1", "desc": "d", "params": {"text": "red shoes"}, "closes": "page"}],
        "claims": [{"subgoal": "s1", "kind": "navigated", "param": "red shoes"}],
    })  # fmt: skip
    seen: list[list[str]] = []

    def ask(_system: list[str], blocks: list[str]) -> str:
        seen.append(blocks)
        return bad

    with pytest.raises(ControllerError):
        decide(ask, task_goal=GOAL, subgoal_lines=[], action_log=[], subgoal_status=[],
               window_title="t", listing_rows=[], listed_ids=frozenset(), first_turn=True,
               task_fence="taskfence01")  # fmt: skip
    assert seen[1][-1] == repair_line(["param_unknown"])


def test_the_rule_says_a_type_never_gives_both_param_and_text() -> None:
    """The offline eval: "either `param` or `text`" drew 2 of 5 `type_text_and_param` on turn 1."""
    rules = _system()[0]
    assert "A `type` gives `param` or `text`, never both." in rules
    assert "gives either" not in rules


def test_the_rule_says_a_reply_stops_or_asks_never_both() -> None:
    """The [V2.64] eval: 18 of 20 3d-b replies stopped blocked with an ask (`ask_with_stop`)."""
    assert "A reply either stops or asks, never both." in _system()[0]
