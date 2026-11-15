"""#386 step 1: each rejected Decision's shape on the redacted trace, as closed tokens only, so a
rejection like 3d-a's (`param_unknown`, `not_in_step`) can be read without the decide record.
Verbs, keys and claim kinds are the schema's own closed tokens (the parser accepts no other);
a declared param is written as its position among its subgoal's declared params, sorted
(`param:0`), else `param_unknown`: a declared name is model text and may carry the request's words.
Never a value, a name, a desc or any free text."""

from __future__ import annotations

import json
from typing import Any

import pytest

from steerai.loop2.controller import ControllerError, decide, decision_shape
from steerai.loop2.decision import Decision

GOAL = "search the catalog for red shoes"
SUB = {"id": "s1", "desc": "search the catalog", "params": {"query": "red shoes"}, "closes": "page"}


def _decision(**doc: Any) -> Decision:  # Any: a Decision's JSON fields
    return Decision.model_validate(doc)


def test_an_action_is_its_verb_whether_in_step_and_its_param_if_declared() -> None:
    shape = decision_shape(
        _decision(subgoals=[SUB], actions=[
            {"verb": "type", "target": 3, "subgoal": "s1", "param": "query"},
            {"verb": "scroll", "subgoal": "s1"},
            {"verb": "type", "target": 3, "subgoal": "s1", "param": "red shoes words"},
        ]),
        known_subgoals={}, tokens=["not_in_step", "param_unknown"], turn=1, attempt=0,
    )  # fmt: skip
    assert shape["actions"] == [
        {"verb": "type", "in_step": True, "param": "param:0", "text": False, "key": None},
        {"verb": "scroll", "in_step": False, "param": None, "text": False, "key": None},
        {"verb": "type", "in_step": True, "param": "param_unknown", "text": False, "key": None},
    ]
    assert (shape["turn"], shape["attempt"]) == (1, 0)
    assert shape["rejections"] == ["not_in_step", "param_unknown"]


def test_a_claim_is_its_kind_whether_checked_in_step_and_its_param_if_declared() -> None:
    shape = decision_shape(
        _decision(claims=[{"subgoal": "s1", "kind": "content_present", "param": "q"},
                          {"subgoal": "s1", "kind": "value", "param": "query"}]),
        known_subgoals={"s1": frozenset({"query"})}, tokens=["not_in_step"], turn=2, attempt=1,
    )  # fmt: skip
    assert shape["claims"] == [
        {"kind": "content_present", "in_step": False, "param": "param_unknown", "evidence": 0},
        {"kind": "value", "in_step": True, "param": "param:0", "evidence": 0},
    ]


def test_the_subgoals_are_counted_with_their_closing_kind_never_their_desc_or_values() -> None:
    shape = decision_shape(_decision(subgoals=[SUB]), known_subgoals={}, tokens=[], turn=1,
                           attempt=0)  # fmt: skip
    assert shape["subgoals"] == [{"closes": "page", "params": 1}]
    text = json.dumps(shape)
    for leaked in ("red shoes", "search the catalog", "catalog"):
        assert leaked not in text


def test_typed_text_is_only_said_to_be_present() -> None:
    shape = decision_shape(
        _decision(actions=[{"verb": "type", "target": 3, "subgoal": "s1", "text": "red shoes"},
                           {"verb": "press", "subgoal": "s1", "key": "Enter"}]),
        known_subgoals={"s1": frozenset({"query"})}, tokens=[], turn=2, attempt=0,
    )  # fmt: skip
    assert shape["actions"][0]["text"] is True
    assert shape["actions"][1]["key"] == "Enter"
    assert "red shoes" not in json.dumps(shape)


def test_a_goal_word_used_as_a_param_name_never_reaches_the_record() -> None:
    """Sentinel: `red_shoes` passes the name pattern, but it is the request's words; only its
    position is written, and no goal word appears anywhere in the record."""
    sub = {"id": "s1", "desc": "x", "params": {"red_shoes": "red shoes", "colour": "red"},
           "closes": "page"}  # fmt: skip
    shape = decision_shape(
        _decision(subgoals=[sub],
                  claims=[{"subgoal": "s1", "kind": "value", "param": "red_shoes"}],
                  actions=[{"verb": "type", "target": 3, "subgoal": "s1", "param": "colour"}]),
        known_subgoals={}, tokens=[], turn=1, attempt=0,
    )  # fmt: skip
    assert shape["claims"][0]["param"] == "param:1"  # sorted: colour, red_shoes
    assert shape["actions"][0]["param"] == "param:0"
    text = json.dumps(shape)
    for word in ("red", "shoes", "colour"):
        assert word not in text


def test_the_positions_follow_the_sorted_declared_names_from_an_earlier_turn() -> None:
    shape = decision_shape(
        _decision(claims=[{"subgoal": "s1", "kind": "value", "param": "zeta"}]),
        known_subgoals={"s1": frozenset({"zeta", "alpha"})}, tokens=[], turn=2, attempt=0,
    )  # fmt: skip
    assert shape["claims"][0]["param"] == "param:1"


def test_ask_and_stop_are_flags() -> None:
    shape = decision_shape(_decision(ask="which one?", stop=None), known_subgoals={},
                           tokens=["subgoals_missing"], turn=1, attempt=0)  # fmt: skip
    assert (shape["ask"], shape["stop"]) == (True, None)
    assert "which one" not in json.dumps(shape)


# ---- the controller hands each rejected attempt's shape to its listener --------------------------


def test_decide_hands_each_rejected_attempt_shape_to_on_rejected() -> None:
    bad = json.dumps({"subgoals": [SUB], "claims": [{"subgoal": "s1", "kind": "content_present",
                                                     "param": "q"}]})  # fmt: skip
    seen: list[dict[str, Any]] = []
    with pytest.raises(ControllerError):
        decide(
            lambda _s, _b: bad, task_goal=GOAL, subgoal_lines=[], action_log=[],
            subgoal_status=[], window_title="Shop", listing_rows=[], listed_ids=frozenset(),
            first_turn=True, task_fence="taskfence01", on_rejected=seen.append, turn=1,
        )  # fmt: skip
    assert [s["attempt"] for s in seen] == [0, 1]  # the original and its one repair
    assert all(s["rejections"] for s in seen)
    assert all(s["claims"][0]["kind"] == "content_present" for s in seen)


# ---- the decide port numbers the turns; LiveWorld writes the shapes on the redacted trace -------


def test_the_decide_port_numbers_its_turns_for_the_shapes() -> None:
    from steerai.loop2.decide_port import DecideCalls, Reply, TaskGenerations  # noqa: PLC0415
    from steerai.loop2.loop import TokenUsage  # noqa: PLC0415
    from steerai.loop2.prompt_inputs import make_decide  # noqa: PLC0415
    from tests.loop2.test_listing_wiring import _view  # noqa: PLC0415

    bad = json.dumps({"subgoals": [SUB], "claims": [{"subgoal": "s1", "kind": "content_present"}]})

    def provider(_system: list[str], _blocks: list[str]) -> Reply:
        return Reply(bad, "end_turn", TokenUsage(10, 5))

    calls = DecideCalls(provider, interrupted=lambda: None, generations=TaskGenerations(),
                        late_error=lambda _t: None)  # fmt: skip
    seen: list[dict[str, Any]] = []
    port = make_decide(calls, goal=GOAL, task_fence="taskfence01", on_rejected=seen.append)
    for _ in range(2):
        with pytest.raises(ControllerError):
            port(_view())
    assert [(s["turn"], s["attempt"]) for s in seen] == [(1, 0), (1, 1), (2, 0), (2, 1)]


def test_live_world_writes_each_rejected_shape_on_the_redacted_trace() -> None:
    from steerai.loop2.runner import run_task  # noqa: PLC0415
    from tests.action.fakes import TYPE_IDS  # noqa: PLC0415
    from tests.loop2.test_live_world import Platform, _Handle, _world  # noqa: PLC0415
    from tests.loop2.world_fakes import PAGE, task  # noqa: PLC0415

    platform = Platform()
    platform.handles = [_Handle(control_type=TYPE_IDS.get(e.role, TYPE_IDS["Button"]),
                                name=e.name) for e in PAGE]  # fmt: skip
    platform.decision_doc = {
        "subgoals": [SUB],
        "claims": [{"subgoal": "s1", "kind": "content_present", "param": "q"}],
    }
    world, _ = _world(platform)
    run_task(task(), world.ports(task()))
    world.close()
    lines = [json.loads(line) for line in world._config.trace.lines]  # type: ignore[attr-defined]  # a ListTrace
    rejected = [r for r in lines if r.get("event") == "decision_rejected"]
    assert rejected, "every rejected attempt is written"
    assert rejected[0]["claims"][0] == {"kind": "content_present", "in_step": False,
                                        "param": "param_unknown", "evidence": 0}  # fmt: skip
    assert "red shoes" not in json.dumps(rejected)
