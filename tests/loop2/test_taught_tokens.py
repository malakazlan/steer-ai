"""[V2.68] Two (3): every refusal the model can avoid by following a stated rule is stated before
it errs. Each model-fault token (every `ValidateToken`, and every runner `RefusalWord` whose kind
is `model`) maps to a line of the system prompt (its rules or its listing legend) or to a
constraint of the schema the prompt shows; a token with no entry fails. The repair legend does
not count: it is shown only after a rejection.

Excluded, with a reason each (`EXCLUDED`): a step fact the model cannot avoid by a rule, and
step 4 tokens no step 3 code produces (`loop2.consent` is not wired into the runner)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

import pytest

from steerai.loop2.controller import SYSTEM_RULES, _prompt_schema
from steerai.loop2.decision import (
    MAX_ACTIONS,
    MAX_DESC_CHARS,
    MAX_SUBGOALS,
    PARAM_NAME,
    SUBGOAL_ID,
)
from steerai.loop2.history import REFUSAL_TOKENS
from steerai.loop2.runner import REFUSAL_KIND

Schema = Mapping[str, Any]  # Any: a JSON schema document


def _defs(first: bool = True) -> Mapping[str, Any]:  # Any: JSON schema parts
    defs: Mapping[str, Any] = _prompt_schema(first_turn=first)["$defs"]  # Any: JSON parts
    return defs


def _requires(variant: str, field: str) -> Callable[[], bool]:
    return lambda: field in _defs()[variant]["required"]


def _omits(variant: str, field: str) -> Callable[[], bool]:
    def check() -> bool:
        part = _defs()[variant]
        return field not in part["properties"] and part["additionalProperties"] is False

    return check


def _later_turn_has_no_subgoals() -> bool:
    return "subgoals" not in _prompt_schema(first_turn=False)["properties"]


def _type_text_or_param() -> bool:
    return bool(
        _defs()["Action_type"]["oneOf"] == [{"required": ["text"]}, {"required": ["param"]}]
    )


def _top(field: str, key: str, value: object) -> Callable[[], bool]:
    def check() -> bool:
        prop = _prompt_schema(first_turn=True)["properties"][field]
        branch = next((b for b in prop.get("anyOf", [prop]) if b.get("type") != "null"), prop)
        return bool(branch.get(key) == value)

    return check


def _subgoal_id_pattern() -> bool:
    defs = _defs()
    referenced = defs["SubgoalDoc"]["properties"]["id"] == {"$ref": "#/$defs/SubgoalId"}
    return referenced and bool(defs["SubgoalId"]["pattern"] == f"^{SUBGOAL_ID.pattern}$")


def _param_names() -> bool:
    params = _defs()["SubgoalDoc"]["properties"]["params"]
    return bool(params["propertyNames"]["pattern"] == f"^{PARAM_NAME.pattern}$")


def _params_needed() -> bool:
    return bool(_defs()["SubgoalDoc"]["properties"]["params"].get("minProperties") == 1)


def _desc_length() -> bool:
    return bool(_defs()["SubgoalDoc"]["properties"]["desc"]["maxLength"] == MAX_DESC_CHARS)


Taught = tuple[str, str] | Callable[[], bool]
"""A rule: ("rule", the exact words of the system prompt that state it); a schema constraint: a
check on the schema the first turn shows."""

TAUGHT: Final[Mapping[str, Taught]] = {
    "ask_empty": _top("ask", "minLength", 1),
    "ask_with_actions": ("rule", "A reply either stops or asks, never both, and then has no "),
    "ask_with_stop": ("rule", "A reply either stops or asks, never both"),
    "desc_bad": _desc_length,
    "empty_decision": ("rule", "Every reply acts, claims, asks or stops."),
    "param_name_bad": _param_names,
    "param_not_span": (
        "rule",
        "When you declare a subgoal, each param's value is words copied exactly from the "
        "request, never an element's name.",
    ),
    "state_without_param": _params_needed,
    "stop_with_actions": ("rule", "A reply either stops or asks, never both, and then has no "),
    "subgoal_duplicate": ("rule", "Subgoal ids are unique"),
    "subgoal_id_bad": _subgoal_id_pattern,
    "subgoal_unknown": ("rule", "actions and claims name a declared subgoal id"),
    "subgoals_missing": ("rule", "A first reply that acts or claims declares its subgoals."),
    "text_bad": ("rule", "`type` text is one line of words from the request exactly"),
    "too_many_actions": _top("actions", "maxItems", MAX_ACTIONS),
    "too_many_subgoals": _top("subgoals", "maxItems", MAX_SUBGOALS),
    "value_without_param": _params_needed,
    "commit_cannot_apply": _omits("Action_wait", "commit"),
    "commit_undeclared": ("rule", "Declare `commit` for any action that changes someone else's"),
    "focus_needs_field": ("rule", "`focus` is for text fields only"),
    "id_not_listed": ("rule", "Act only on ids in the current listing."),
    "intake_insufficient": (
        "rule",
        "refused:intake_insufficient means your subgoals would not finish the request",
    ),
    "key_missing": _requires("Action_press", "key"),
    "not_own_text": ("rule", "Backspace edits only text you typed"),
    "option_missing": _requires("Action_choose", "option"),
    "page_without_param": ("rule", "A page subgoal needs at least one param."),
    "param_missing": _requires("Claim_value", "param"),
    "param_unknown": ("rule", "`param` names one of its subgoal's params by its name"),
    "repeat_send": ("rule", "Never repeat an action that had no effect."),
    "subgoals_after_first": _later_turn_has_no_subgoals,
    "target_missing": _requires("Action_click", "target"),
    "target_on_untargeted_verb": _omits("Action_press", "target"),
    "text_missing": _type_text_or_param,
    "type_text_and_param": ("rule", "A `type` gives `param` or `text`, never both."),
    "wanted_missing": _requires("Claim_state", "wanted"),
}

EXCLUDED: Final[Mapping[str, str]] = {
    "done_already": "a step fact (the admission found the action already done)",
    "commit_conflict": "step 4: only `loop2.consent` gives it, and the runner does not use it",
    "no_strict_shape": "step 4: only `loop2.consent` gives it, and the runner does not use it",
}
"""`done_already` is a `model` fault by [V2.74] (repeating a delivered commit is the model's
choice) and stays excluded: no rule avoids the admission's finding. The step 4 two
are taught when the runner wires `consent` (the tripwire below fails then)."""


def _model_faults() -> set[str]:
    return {t for t in REFUSAL_TOKENS if REFUSAL_KIND[t] == "model"}


def test_every_model_fault_is_taught_or_excluded_with_a_reason() -> None:
    untaught = sorted(_model_faults() - set(TAUGHT) - set(EXCLUDED))
    assert untaught == [], f"no rule or schema line tells the model: {untaught}"


def test_no_entry_names_a_token_that_is_not_a_model_fault() -> None:
    faults = _model_faults()
    assert set(TAUGHT) <= faults
    assert set(EXCLUDED) <= faults
    assert not set(TAUGHT) & set(EXCLUDED)


@pytest.mark.parametrize("token", sorted(TAUGHT))
def test_each_entry_is_really_in_the_prompt_or_the_schema(token: str) -> None:
    taught = TAUGHT[token]
    if isinstance(taught, tuple):
        assert taught[1] in SYSTEM_RULES, token
    else:
        assert taught(), token


def test_the_step_4_exclusions_hold_only_while_the_runner_does_not_use_consent() -> None:
    """Tripwire: once the runner imports `loop2.consent`, its tokens must be taught."""
    import inspect  # noqa: PLC0415

    from steerai.loop2 import runner  # noqa: PLC0415

    assert "consent" not in inspect.getsource(runner)


PARTLY_TAUGHT: Final[Mapping[str, str]] = {
    "desc_bad": (
        "#449: its length is taught by the schema (`maxLength`); its fence and control-character "
        "half stays validator-only. A desc in the model's own words holds neither unless it "
        "copies a fence line (an injection) or writes a raw control character; teaching it "
        "would change the prompt's wording (a controller version) for a fault no stage run is "
        "known to have hit (unmeasured)."
    ),
}
"""Tokens with more than one rule, only some of which are taught: each names what is taught,
what stays with the validator, and why."""


def test_a_partly_taught_token_is_taught_and_says_what_is_not() -> None:
    assert set(PARTLY_TAUGHT) <= set(TAUGHT)
    assert all(reason.strip() for reason in PARTLY_TAUGHT.values())


def test_the_desc_halves_left_to_the_validator_are_still_refused() -> None:
    """#449: the untaught half is enforced, so leaving it untaught costs a repair, never a pass."""
    from steerai.loop2.decision import Decision, validate  # noqa: PLC0415

    goal = "turn on Dark mode"
    for desc in ("line one\nline two", "a fence f1f1f1f1 inside"):
        decision = Decision.model_validate({
            "subgoals": [{"id": "s1", "desc": desc, "params": {"what": "Dark mode"},
                          "closes": "state"}],
            "stop": "cannot",
        })  # fmt: skip
        tokens = validate(decision, listed_ids=frozenset(), goal=goal, first_turn=True,
                          fences=frozenset({"f1f1f1f1"}))  # fmt: skip
        assert "desc_bad" in tokens


def test_every_refusal_token_declares_its_kind() -> None:
    """[V2.74] (#446): no default; a token without a kind is a missing fact, not a model fault."""
    assert sorted(REFUSAL_TOKENS - set(REFUSAL_KIND)) == []


@pytest.mark.parametrize(
    "token",
    ["not_foreground", "screen_changed_during_ask", "composer_unreadable", "composer_differs"],
)
def test_a_consent_world_fact_is_world_and_names_the_ending_over_a_model_fault(token: str) -> None:
    from steerai.loop2.runner import refusal_of  # noqa: PLC0415

    assert REFUSAL_KIND[token] == "world"
    assert token not in _model_faults()
    assert refusal_of(["id_not_listed", token]) == ("world", token)
    assert refusal_of([token, "id_not_listed"]) == ("world", token)


def test_done_already_stays_a_model_fault() -> None:
    from steerai.loop2.runner import refusal_of  # noqa: PLC0415

    assert refusal_of(["done_already"]) == ("model", "done_already")
