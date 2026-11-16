"""[V2.74] (#475): one Decision's claims are judged in the subgoals' declaration order, not the
order the model listed them, so a later subgoal's claim cannot rebase an earlier one out of its
event (`_rebase`); the auto proof already runs first on every full read, in declaration order."""

from __future__ import annotations

from steerai.loop2.decision import Claim, Decision, SubgoalDoc
from steerai.loop2.runner import _Run, run_task
from steerai.loop2.task import GoalSpan, Subgoal
from tests.loop2.test_auto_proof_every_observation import RESULTS, START
from tests.loop2.test_navigated import page
from tests.loop2.world_fakes import GO, GOAL, RESULT, URL, World, act, report, task

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


# ---- end to end through run_task ---------------------------------------------------------------

SEARCH_DOC = SubgoalDoc(id="s1", desc="search", params={"text": "red shoes"}, closes="page")
OPEN_DOC = SubgoalDoc(id="s2", desc="open the page", params={"text": "red shoes"}, closes="page")
RESULTS_PAGE = page([], url=URL + "?q=red%20shoes", title="red shoes")
ITEM_PAGE = page([RESULT], url=URL + "item/red%20shoes", title="Red shoes - Item")


def _scripted(*, opens: bool) -> World:
    """Turn 1 declares both and searches (the after-action read shows the results); turn 2 claims
    s2 on that page and, if `opens`, opens the page."""
    second = Decision(claims=[Claim(subgoal="s2", kind="navigated")])
    if opens:
        second = Decision(
            claims=[Claim(subgoal="s2", kind="navigated")], actions=[act("click", target=GO.id)]
        )
    world = World(
        [
            Decision(subgoals=[SEARCH_DOC, OPEN_DOC], actions=[act("click", target=GO.id)]),
            second,
            Decision(stop="blocked"),
        ],
        effects=[report("navigated"), report("navigated")],
    )
    world.timeline = [(1, RESULTS_PAGE), (2, ITEM_PAGE)] if opens else [(1, RESULTS_PAGE)]
    return world


def _seen(world: World) -> list[str]:
    return [t for v in world.views for t in v.history]


def test_the_auto_proof_closes_the_search_before_the_claim_on_the_same_read() -> None:
    world = _scripted(opens=False)
    run_task(task(), world.ports())
    seen = _seen(world)
    assert "proven:s1:title" in seen
    assert not any(t.startswith("proven:s2:") for t in seen)
    assert any(t.startswith("claim:s2:") for t in seen)


def test_the_rebased_open_page_closes_when_it_is_opened() -> None:
    world = _scripted(opens=True)
    outcome = run_task(task(), world.ports())
    assert "proven:s1:title" in _seen(world)
    assert outcome.status == "done"  # s2 closed by the page it opened, never by its early claim
    assert len(world.views) == 2  # the task ended on turn 2's action: no third decision
    assert outcome.rebased_open == 0


def test_a_rebased_page_never_opened_ends_the_task_open_and_counted() -> None:
    outcome = run_task(task(), _scripted(opens=False).ports())
    assert outcome.status != "done"
    assert outcome.rebased_open == 1
