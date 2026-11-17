"""#422: a value or state claim cites exactly one element. One citing none, or more than one, is
refused with the claim check's own count token (`evidence_missing`, `too_much_evidence`) before
anything reads its first element; it never raises out of the task."""

from __future__ import annotations

import pytest

from steerai.loop2.decision import Claim, ClaimKind, Closes, Decision, SubgoalDoc
from steerai.loop2.runner import run_task
from tests.loop2.world_fakes import FIELD, GO, World, task

CASES: list[tuple[ClaimKind, Closes]] = [("value", "value"), ("state", "state")]


def _claim(kind: ClaimKind, evidence: list[int]) -> Claim:
    if kind == "state":
        return Claim(subgoal="s1", kind="state", evidence=evidence, param="text")
    return Claim(subgoal="s1", kind="value", evidence=evidence, param="text")


def _run(kind: ClaimKind, closes: Closes, evidence: list[int]) -> World:
    subs = [
        SubgoalDoc(
            id="s1",
            desc="x",
            params={"text": "red shoes"},
            closes=closes,
            wanted="on" if closes == "state" else None,
        )
    ]
    world = World([Decision(subgoals=subs, claims=[_claim(kind, evidence)])])
    outcome = run_task(task(), world.ports())
    assert outcome.status != "done"
    return world


@pytest.mark.parametrize(("kind", "closes"), CASES)
def test_a_claim_citing_nothing_is_refused_as_evidence_missing(
    kind: ClaimKind, closes: Closes
) -> None:
    world = _run(kind, closes, [])
    assert "claim:s1:evidence_missing" in world.views[-1].history


@pytest.mark.parametrize(("kind", "closes"), CASES)
def test_a_claim_citing_two_elements_is_refused_as_too_much_evidence(
    kind: ClaimKind, closes: Closes
) -> None:
    world = _run(kind, closes, [FIELD.id, GO.id])
    assert "claim:s1:too_much_evidence" in world.views[-1].history
