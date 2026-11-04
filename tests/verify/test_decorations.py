"""Decorated names (VF-34, design 5.10 [v1.7]): an app shows the step's content inside a fixed
frame ("<query> - Google Search"). The plan names a decoration from the code-owned registry;
code fills the full name from the content parameter. The model never writes the frame."""

from __future__ import annotations

import json

import pytest

from steerai.orchestrator.plan_io import PlanError, plan_from_json, plan_to_json
from steerai.verify.decorations import DECORATIONS, decorate
from steerai.verify.predicates import Appeared
from steerai.verify.strength import Params, strength
from steerai.verify.t1 import evaluate
from tests.orchestrator.test_plan_io import _doc
from tests.verify.fakes import BASE_ROWS, WINDOW, after_with, before, build

QUERY = "steer ai computer use agent"
RESULTS_DOC = (40, "Document", f"{QUERY} - Google Search", (42, 90), WINDOW[0])


def test_the_registry_is_code_owned_and_fills_the_content_in() -> None:
    assert "google_search" in DECORATIONS
    assert decorate("google_search", QUERY) == f"{QUERY} - Google Search"
    with pytest.raises(ValueError, match="decoration"):
        decorate("nope", QUERY)


def test_a_decorated_appeared_matches_the_full_decorated_name() -> None:
    predicate = Appeared(role="Document", name_pattern=QUERY, decoration="google_search")
    assert predicate.name_match == f"{QUERY} - Google Search"
    assert predicate.name_pattern == QUERY
    verdict = evaluate(predicate, before(), after_with(RESULTS_DOC))
    assert verdict.holds is True, verdict.evidence


def test_an_undecorated_appeared_does_not_match_a_decorated_name() -> None:
    verdict = evaluate(
        Appeared(role="Document", name_pattern=QUERY), before(), after_with(RESULTS_DOC)
    )
    assert verdict.holds is False


def test_an_unknown_decoration_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="decoration"):
        Appeared(role="Document", name_pattern=QUERY, decoration="made_up")


def test_a_decorated_appeared_is_strong_when_its_pattern_is_the_text() -> None:
    predicate = Appeared(role="Document", name_pattern=QUERY, decoration="google_search")
    assert strength(predicate, "R2", Params(target_id=1, text=QUERY)).ok is True
    assert strength(predicate, "R2", Params(target_id=1, text="other")).ok is False


def test_the_file_form_carries_the_decoration_key_and_round_trips() -> None:
    expect = {"appeared": {"role": "Document", "name": QUERY, "decoration": "google_search"}}
    doc = _doc()
    doc["steps"][0]["expect"] = expect
    plan = plan_from_json(json.dumps(doc))
    assert plan.steps[0].expect == Appeared(
        role="Document", name_pattern=QUERY, decoration="google_search"
    )
    assert json.loads(plan_to_json(plan))["steps"][0]["expect"] == expect
    doc["steps"][0]["expect"] = {"appeared": {"role": "Document", "name": QUERY, "decoration": "x"}}
    with pytest.raises(PlanError):
        plan_from_json(json.dumps(doc))


# ---- replacement (navigation) form ----------------------------------------------------------

OLD_DOC = (40, "Document", "Google", (42, 90), WINDOW[0])
NEW_DOC = (40, "Document", f"{QUERY} - Google Search", (42, 91), WINDOW[0])


def _nav(*, replaces: bool) -> Appeared:
    return Appeared(
        role="Document", name_pattern=QUERY, decoration="google_search", replaces=replaces
    )


def test_a_replacing_appeared_holds_when_the_predecessor_is_gone() -> None:
    before_snap = build([*BASE_ROWS, OLD_DOC], snapshot_id="before")
    after_snap = build([*BASE_ROWS, NEW_DOC], snapshot_id="after", taken_at=1.0)
    verdict = evaluate(_nav(replaces=True), before_snap, after_snap)
    assert verdict.holds is True, verdict.evidence
    assert verdict.evidence["replaced"] == 1


def test_without_replaces_a_vanished_predecessor_is_a_scope_shift() -> None:
    before_snap = build([*BASE_ROWS, OLD_DOC], snapshot_id="before")
    after_snap = build([*BASE_ROWS, NEW_DOC], snapshot_id="after", taken_at=1.0)
    verdict = evaluate(_nav(replaces=False), before_snap, after_snap)
    assert verdict.holds is False
    assert verdict.evidence["reason"] == "scope_shifted"


def test_a_replacing_appeared_needs_exactly_one_predecessor_gone() -> None:
    before_snap = build([*BASE_ROWS, OLD_DOC], snapshot_id="before")
    both = build([*BASE_ROWS, OLD_DOC, (41, *NEW_DOC[1:])], snapshot_id="after", taken_at=1.0)
    verdict = evaluate(_nav(replaces=True), before_snap, both)
    assert verdict.holds is False
    assert verdict.code is None, "still loading is no label, not a contradiction"
    assert verdict.evidence["reason"] == "not_replaced"


def test_replaces_round_trips_in_the_file_form() -> None:
    expect = {
        "appeared": {
            "role": "Document",
            "name": QUERY,
            "decoration": "google_search",
            "replaces": True,
        }
    }
    doc = _doc()
    doc["steps"][0]["expect"] = expect
    plan = plan_from_json(json.dumps(doc))
    assert plan.steps[0].expect == _nav(replaces=True)
    assert json.loads(plan_to_json(plan))["steps"][0]["expect"] == expect
