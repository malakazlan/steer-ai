"""[V2.67] D, the proof part: every page proof attempt reaches the turn trace with every condition,
not only the verdict. That covers the read it ran on (its kind, tier, stable, growing, young),
why it was skipped, the runner's change gate, the verifier's change classes and its
no-navigation refusals, each param's carriers (held now, absent before), the content signal and
what met it, and every guard that rejected a row. A subgoal that did not close says which
condition failed (the 3d-a run 3 question)."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from steerai.loop2.decision import Decision, SubgoalDoc
from steerai.loop2.runner import run_task
from steerai.loop2.turn_trace import closed
from tests.loop2.test_auto_proof_every_observation import RESULTS, START
from tests.loop2.test_navigated import page
from tests.loop2.test_turn_trace import _into
from tests.loop2.world_fakes import FIELD, RESULT, URL, World, act, report, task

SUBS = [SubgoalDoc(id="s1", desc="results", params={"text": "red shoes"}, closes="page")]


def _attempts(world: World, **ports: Any) -> list[dict[str, Any]]:  # Any: JSON
    records: list[dict[str, Any]] = []
    run_task(task(), dataclasses.replace(world.ports(), turn_trace=_into(records), **ports))
    assert all(closed(r) for r in records)
    written = json.loads(json.dumps(records))  # as the trace writes it
    return [p for r in written for p in r["proofs"]]


def _after_action(attempts: list[dict[str, Any]]) -> dict[str, Any]:  # Any: JSON
    (proof,) = [p for p in attempts if p["read_kind"] == "after_action"]
    return proof


def _typed(after: Any = None, *seen: Any) -> World:  # Any: Observed
    world = World(
        [Decision(subgoals=SUBS, actions=[act("type", target=FIELD.id, param="text")]),
         Decision(stop="blocked"), Decision(stop="blocked")],
        effects=[report("own_value", "url_changed_in_place")],
        screen=START,
        observations=[START, START, *seen],
    )  # fmt: skip
    world.after = after
    return world


def test_an_after_action_proof_names_its_read_gate_carriers_and_content() -> None:
    proof = _after_action(_attempts(_typed(RESULTS)))
    assert (proof["read_kind"], proof["subgoal"]) == ("after_action", 1)
    assert proof["read"] == {"tier": "full", "stable": True, "growing": False, "young": False}
    assert (proof["skipped"], proof["gate"]) == (None, "url")
    assert (proof["change"], proof["change_by"]) == (True, ["url_changed_in_place"])
    assert (proof["proven"], proof["proven_how"]) == (True, "results_with_url")
    assert (proof["content"], proof["content_by"]) == (True, "results_with_url")
    (param,) = proof["params"]
    held = {c["carrier"]: (c["held"], c["absent_before"]) for c in param["carriers"]}
    assert param["param"] == 1
    assert held["url_param"] == (True, True)
    assert held["results"] == (True, True)


def test_a_late_result_is_proven_on_the_stale_re_read_and_says_so() -> None:
    attempts = _attempts(_typed(None, RESULTS))
    kinds = [(p["read_kind"], p["proven"]) for p in attempts]
    assert ("stale_reread", True) in kinds


def test_a_read_with_no_change_names_the_gate_and_proves_nothing() -> None:
    attempts = _attempts(
        _typed(dataclasses.replace(page([RESULT], url=URL), focus=FIELD.runtime_id))
    )
    proof = _after_action(attempts)
    assert (proof["gate"], proof["change"], proof["proven"]) == (None, False, False)
    assert proof["params"] == []


def test_an_unread_address_is_named_as_the_refusal() -> None:
    unread = dataclasses.replace(RESULTS, document_url=None)
    proof = _after_action(_attempts(_typed(unread)))
    assert proof["refused"] == ["url_unread"]
    assert proof["proven"] is False


def test_a_poll_read_is_recorded_as_skipped() -> None:
    poll = dataclasses.replace(RESULTS, tier="poll")
    attempts = _attempts(_typed(None, poll))
    assert any(p["skipped"] == "poll" for p in attempts)


def test_a_guard_that_rejected_a_row_is_named() -> None:
    """The only row holding the words sits under a Menu (a suggestion list): the guard names
    `under_suggestions` and nothing closes."""
    from tests.loop2.world_fakes import element  # noqa: PLC0415 - this test only

    menu = element(50, "Menu", "Suggestions", (9, 50), 1)
    item = element(51, "MenuItem", "red shoes", (9, 51), 50)
    suggested = dataclasses.replace(page([menu, item]), focus=FIELD.runtime_id)
    proof = _after_action(_attempts(_typed(suggested)))
    assert proof["proven"] is False
    assert "under_suggestions" in proof["guards"]


def test_the_attempts_carry_no_page_text() -> None:
    records: list[dict[str, Any]] = []
    world = _typed(RESULTS)
    run_task(task(), dataclasses.replace(world.ports(), turn_trace=_into(records)))
    written = json.dumps([r["proofs"] for r in records])
    for name in {e.name for e in RESULTS.snapshot.elements if e.name} | {"red shoes", "s1", "text"}:
        assert name not in written
