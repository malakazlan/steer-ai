"""[V2.74] (#475): one Decision's claims are judged in the subgoals' declaration order, not the
order the model listed them, so a later subgoal's claim cannot rebase an earlier one out of its
event (`_rebase`); the auto proof already runs first on every full read, in declaration order."""

from __future__ import annotations

from steerai.loop2.decision import Claim
from steerai.loop2.runner import _Run
from steerai.loop2.task import GoalSpan, Subgoal
from tests.loop2.test_auto_proof_every_observation import RESULTS, START
from tests.loop2.world_fakes import GOAL, World, task

_START = GOAL.index("red shoes")
SPAN = GoalSpan("red shoes", _START, _START + len("red shoes"))
SEARCH = Subgoal(id="s1", desc="search", params={"text": SPAN}, closes="page")
OPEN = Subgoal(id="s2", desc="open the page", params={"text": SPAN}, closes="page")
CLAIM_S1 = Claim(subgoal="s1", kind="navigated")
CLAIM_S2 = Claim(subgoal="s2", kind="navigated")


def _run() -> _Run:
    run = _Run(task(), World([]).ports())
    run.subgoals = (SEARCH, OPEN)
    run.first_full = START
    return run


def test_claims_listed_out_of_order_are_judged_in_declaration_order() -> None:
    run = _run()
    run._claims([CLAIM_S2, CLAIM_S1], RESULTS)
    assert run.proven == {"s1"}
    assert run.baselines == {"s2": RESULTS}


def test_a_rebased_subgoal_is_refused_not_newer_and_stays_open() -> None:
    run = _run()
    run._claims([CLAIM_S1, CLAIM_S2], RESULTS)
    assert run.proven == {"s1"}
    assert "s2" not in run.proven


def test_a_rebased_subgoal_that_ends_open_is_counted() -> None:
    run = _run()
    run._claims([CLAIM_S1, CLAIM_S2], RESULTS)
    outcome = run._outcome("stopped", None)
    assert outcome.rebased_open == 1


def test_a_rebased_subgoal_that_closes_later_is_not_counted() -> None:
    run = _run()
    run._claims([CLAIM_S1], RESULTS)
    run.proven.add("s2")
    assert run._outcome("stopped", None).rebased_open == 0
