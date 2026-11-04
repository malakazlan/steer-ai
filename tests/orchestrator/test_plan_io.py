"""The plan file codec: a process boundary (RN-01/04/05/19/20/26/30/31/32/33/47/50/54)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from steerai.orchestrator.contracts import Plan
from steerai.orchestrator.plan_io import (
    MAX_DEPTH,
    MAX_PLAN_BYTES,
    PlanError,
    plan_from_bytes,
    plan_from_json,
    plan_to_json,
)
from steerai.verify.predicates import (
    All,
    AnyOf,
    Appeared,
    CountDelta,
    Disappeared,
    TitleMatches,
    WindowExists,
    WindowGone,
    Within,
)

CONVERSATION = {"landmark": "Conversation"}


def _doc(**overrides: Any) -> dict[str, Any]:
    conversation = dict(CONVERSATION)  # a fresh copy: tests mutate what they get
    doc: dict[str, Any] = {
        "goal": "send hello there to Amir",
        "subgoals": [
            {
                "id": "g1",
                "desc": "hello sent",
                "condition": {
                    "appeared": {"role": "Text", "name": "hello there", "within": conversation}
                },
            }
        ],
        "steps": [
            {
                "id": "focus",
                "subgoal": "g1",
                "verb": "focus",
                "target": {"role": "Edit", "name": "Message"},
                "risk": "R0",
            },
            {
                "id": "type",
                "subgoal": "g1",
                "verb": "type",
                "target": {"role": "Edit", "name": "Message"},
                "text": "hello there",
                "risk": "R1",
            },
            {
                "id": "send",
                "subgoal": "g1",
                "verb": "send",
                "target": {"role": "Button", "name": "Send", "automation_id": "btnSend"},
                "risk": "R2",
                "params": {
                    "text": "hello there",
                    "composer_role": "Edit",
                    "composer_name": "Message",
                    "scope": "Conversation",
                },
                "preapproved_by_request": True,
                "text_provenance": "verbatim",
            },
        ],
    }
    doc.update(overrides)
    return doc


def _error(doc: Any) -> PlanError:
    with pytest.raises(PlanError) as info:
        plan_from_json(json.dumps(doc))
    return info.value


# ---- the happy path ------------------------------------------------------------------------


def test_a_plan_file_becomes_a_validated_plan() -> None:
    plan = plan_from_json(json.dumps(_doc()))
    assert isinstance(plan, Plan)
    assert [s.verb for s in plan.steps] == ["focus", "type", "send"]
    assert plan.steps[1].text == "hello there"
    target = plan.steps[2].target
    assert target is not None
    assert target.automation_id == "btnSend"
    assert plan.steps[2].params["composer_name"] == "Message"
    assert plan.steps[2].preapproved_by_request is True
    assert plan.subgoals[0].condition == Appeared(
        role="Text", name_pattern="hello there", within=Within(landmark="Conversation")
    )


def test_every_file_predicate_kind_round_trips() -> None:
    condition = {
        "all": [
            {
                "appeared": {
                    "role": "Text",
                    "name": "hi",
                    "within": CONVERSATION,
                    "expected": 2,
                    "order": "prepend",
                }
            },
            {"disappeared": {"role": "ListItem", "name": "Amir", "within": {"landmark": "Chats"}}},
            {
                "any_of": [
                    {"title": {"pattern": "Amir - Chat"}},
                    {"window_exists": {"name": "Save"}},
                ]
            },
            {"window_gone": {"name": "Save As"}},
            {"count_delta": {"role": "ListItem", "delta": -1, "within": {"landmark": "Chats"}}},
        ]
    }
    doc = _doc()
    doc["subgoals"][0]["condition"] = condition
    plan = plan_from_json(json.dumps(doc))
    assert plan.subgoals[0].condition == All(
        (
            Appeared(
                role="Text",
                name_pattern="hi",
                within=Within(landmark="Conversation"),
                expected=2,
                order="prepend",
            ),
            Disappeared(role="ListItem", name_pattern="Amir", within=Within(landmark="Chats")),
            AnyOf((TitleMatches(pattern="Amir - Chat"), WindowExists(name_pattern="Save"))),
            WindowGone(name_pattern="Save As"),
            CountDelta(role="ListItem", delta=-1, within=Within(landmark="Chats")),
        )
    )
    again = plan_from_json(plan_to_json(plan))
    assert again == plan


def test_the_file_form_carries_names_and_text_but_never_element_ids() -> None:
    text = plan_to_json(plan_from_json(json.dumps(_doc())))
    assert "hello there" in text
    assert "Message" in text
    for forbidden in ("element_id", "composer_id", "anchor_id", "excluding_anchor"):
        assert forbidden not in text


def test_a_step_expect_in_the_file_is_accepted_as_a_predicate() -> None:
    doc = _doc()
    doc["steps"][0]["expect"] = {"title": {"pattern": "Amir Khan - Chat"}}
    plan = plan_from_json(json.dumps(doc))
    assert plan.steps[0].expect == TitleMatches(pattern="Amir Khan - Chat")


def test_the_contracts_risk_floor_still_applies() -> None:
    doc = _doc()
    doc["steps"][2]["risk"] = "R1"
    assert plan_from_json(json.dumps(doc)).steps[2].risk == "R2"


def test_names_round_trip_losslessly_after_nfkc() -> None:
    doc = _doc()
    doc["steps"][0]["target"]["name"] = "\uff2dessagé \U0001f600"
    plan = plan_from_json(json.dumps(doc))
    assert plan_from_json(plan_to_json(plan)) == plan


# ---- shapes that are refused, with the field path -----------------------------------------


def test_an_unknown_key_is_refused_wherever_it_appears() -> None:
    assert _error(_doc(extra=1)).path == "$.extra"
    doc = _doc()
    doc["steps"][0]["then"] = "enter"
    assert _error(doc).path == "$.steps[0].then"
    doc = _doc()
    doc["steps"][0]["target"]["box"] = [0, 0, 1, 1]
    assert _error(doc).path == "$.steps[0].target.box"
    doc = _doc()
    doc["subgoals"][0]["condition"] = {"appeared": {"role": "Text", "name": "x", "regex": True}}
    assert _error(doc).path == "$.subgoals[0].condition.appeared.regex"


def test_a_duplicated_key_is_refused_not_last_wins() -> None:
    text = json.dumps(_doc())
    doubled = text.replace('"risk": "R2"', '"risk": "R2", "risk": "R0"', 1)
    with pytest.raises(PlanError, match="duplicate"):
        plan_from_json(doubled)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nan_and_infinity_are_refused(value: str) -> None:
    doc = _doc()
    doc["subgoals"][0]["condition"] = {"count_delta": {"role": "ListItem", "delta": 1}}
    text = json.dumps(doc).replace('"delta": 1', f'"delta": {value}')
    with pytest.raises(PlanError):
        plan_from_json(text)


@pytest.mark.parametrize("delta", [True, 1.0, "1", 10**9])
def test_integers_are_strict(delta: Any) -> None:
    doc = _doc()
    doc["subgoals"][0]["condition"] = {"count_delta": {"role": "ListItem", "delta": delta}}
    assert _error(doc).path == "$.subgoals[0].condition.count_delta.delta"


@pytest.mark.parametrize("kind", ["value_equals", "focused", "state"])
def test_live_predicate_kinds_need_a_snapshot_id_and_are_refused(kind: str) -> None:
    doc = _doc()
    doc["subgoals"][0]["condition"] = {kind: {"target_id": 3}}
    assert _error(doc).path == f"$.subgoals[0].condition.{kind}"


def test_a_predicate_object_has_exactly_one_kind() -> None:
    doc = _doc()
    doc["subgoals"][0]["condition"] = {
        "title": {"pattern": "a"},
        "window_exists": {"name": "b"},
    }
    assert _error(doc).path == "$.subgoals[0].condition"
    doc["subgoals"][0]["condition"] = {}
    assert _error(doc).path == "$.subgoals[0].condition"


@pytest.mark.parametrize(
    ("where", "key"),
    [
        ("target", "element_id"),
        ("params", "composer_id"),
        ("within", "anchor_id"),
        ("appeared", "excluding_anchor"),
    ],
)
def test_snapshot_scoped_ids_are_refused(where: str, key: str) -> None:
    doc = _doc()
    if where == "target":
        doc["steps"][0]["target"][key] = 3
    elif where == "params":
        doc["steps"][2]["params"][key] = "3"
    elif where == "within":
        doc["subgoals"][0]["condition"]["appeared"]["within"] = {key: 3}
    else:
        doc["subgoals"][0]["condition"]["appeared"][key] = 3
    assert key in _error(doc).path


@pytest.mark.parametrize("verb", ["open", "scroll", "click_at", "Send"])
def test_a_verb_without_an_executor_is_refused(verb: str) -> None:
    doc = _doc()
    doc["steps"][0]["verb"] = verb
    assert _error(doc).path == "$.steps[0].verb"


@pytest.mark.parametrize(("verb", "path"), [("type", "semantic"), ("send", "physical")])
def test_a_path_override_the_executors_cannot_serve_is_refused(verb: str, path: str) -> None:
    doc = _doc()
    index = 1 if verb == "type" else 2
    doc["steps"][index]["path"] = path
    assert _error(doc).path == f"$.steps[{index}].path"


def test_a_physical_click_is_the_only_path_override() -> None:
    doc = _doc()
    doc["steps"][0]["verb"] = "click"
    doc["steps"][0]["path"] = "physical"
    assert plan_from_json(json.dumps(doc)).steps[0].path == "physical"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("goal", "send\u200b hello"),
        ("text", "hello\nthere"),
        ("text", "hello\x1b[2Kthere"),
        ("name", "Dele\u200bte"),
        ("name", "\udc80"),
        ("desc", "done\u202e"),
        ("param", "hi\r"),
        ("pattern", "ok\x07"),
    ],
)
def test_control_and_format_characters_are_refused_in_every_string(field: str, value: str) -> None:
    doc = _doc()
    if field == "goal":
        doc["goal"] = value
    elif field == "text":
        doc["steps"][1]["text"] = value
    elif field == "name":
        doc["steps"][0]["target"]["name"] = value
    elif field == "desc":
        doc["subgoals"][0]["desc"] = value
    elif field == "param":
        doc["steps"][2]["params"]["text"] = value
    else:
        doc["subgoals"][0]["condition"]["appeared"]["name"] = value
    with pytest.raises(PlanError, match="character"):
        plan_from_json(json.dumps(doc, ensure_ascii=True))


@pytest.mark.parametrize("step_id", ["", " ", "a b", "x" * 65, "../x", "sé"])
def test_ids_are_plain_ascii_tokens_because_they_reach_the_trace(step_id: str) -> None:
    doc = _doc()
    doc["steps"][0]["id"] = step_id
    assert _error(doc).path == "$.steps[0].id"
    doc = _doc()
    doc["subgoals"][0]["id"] = step_id
    assert _error(doc).path == "$.subgoals[0].id"


def test_params_are_string_to_string_over_the_gates_keys_only() -> None:
    doc = _doc()
    doc["steps"][2]["params"]["scope"] = 3
    assert _error(doc).path == "$.steps[2].params.scope"
    doc = _doc()
    doc["steps"][2]["params"]["recipient"] = "Amir"
    assert _error(doc).path == "$.steps[2].params.recipient"


def test_goal_and_desc_are_capped() -> None:
    assert _error(_doc(goal="g" * 2001)).path == "$.goal"
    doc = _doc()
    doc["subgoals"][0]["desc"] = "d" * 2001
    assert _error(doc).path == "$.subgoals[0].desc"


def test_a_contract_violation_is_reported_with_the_steps_path() -> None:
    doc = _doc()
    doc["steps"][0]["text"] = "focus has no text"
    error = _error(doc)
    assert error.path == "$.steps[0]"
    assert "text" in str(error)
    doc = _doc()
    doc["steps"][0]["subgoal"] = "g9"
    assert _error(doc).path == "$.steps"


def test_nesting_is_capped() -> None:
    inner: dict[str, Any] = {"title": {"pattern": "x"}}
    for _ in range(MAX_DEPTH):
        inner = {"all": [inner, {"title": {"pattern": "y"}}]}
    doc = _doc()
    doc["subgoals"][0]["condition"] = inner
    assert _error(doc).path.startswith("$.subgoals[0].condition")
    inner = {"title": {"pattern": "x"}}
    for _ in range(MAX_DEPTH - 1):
        inner = {"all": [inner, {"title": {"pattern": "y"}}]}
    doc["subgoals"][0]["condition"] = inner
    plan_from_json(json.dumps(doc))


def test_the_document_is_an_object_not_a_list_or_scalar() -> None:
    assert _error([]).path == "$"
    assert _error("plan").path == "$"


# ---- bytes at the boundary -------------------------------------------------------------------


def test_a_bom_is_accepted_and_utf16_is_refused_by_name() -> None:
    text = json.dumps(_doc())
    assert plan_from_bytes(b"\xef\xbb\xbf" + text.encode()) == plan_from_json(text)
    with pytest.raises(PlanError, match="utf-8"):
        plan_from_bytes(text.encode("utf-16"))


def test_the_size_cap_is_exact() -> None:
    text = json.dumps(_doc())
    padded = text.encode() + b" " * (MAX_PLAN_BYTES - len(text.encode()))
    assert len(padded) == MAX_PLAN_BYTES
    plan_from_bytes(padded)
    with pytest.raises(PlanError, match="bytes"):
        plan_from_bytes(padded + b" ")


def test_malformed_json_is_a_plan_error_not_a_traceback() -> None:
    with pytest.raises(PlanError, match="JSON"):
        plan_from_json("{")
