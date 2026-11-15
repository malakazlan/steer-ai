"""#366 P5, rev 4.3a: on `/large.html` the target is listed on the turn it is acted on. Each
listing turn records the handles acted on under it; the final line says how many and whether
every one was among the handles that turn showed; `accept`'s `large_listed` gate reads that, for
the large tasks only, and fails closed on anything unread."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from steerai.loop2 import cli
from steerai.loop2.listing import ListingTurn
from steerai.loop2.loop import TaskOutcome
from steerai.loop2.runner import run_task
from steerai.loop2.stage1 import StageTask, accept
from tests.loop2.test_cli_v2 import Run
from tests.loop2.test_stage1 import DONE, _final, _ok
from tests.loop2.world_fakes import GO, World, act, decision, task

TURN = ListingTurn(eligible=2000, shown=150, cut=1850, keep_at_ceiling=False,
                   goal_at_subcap=False, prompt_cut=0, prompt_tokens=900,
                   listed=frozenset({GO.id}))  # fmt: skip
LARGE = StageTask("lg1", "reserve item 1987", "large", "/large.html", "done")


def test_a_dispatch_marks_its_target_on_the_turn_it_was_decided_on() -> None:
    world = World([decision(act("click", target=GO.id), first=True)])
    world.listing = TURN
    outcome = run_task(task(), world.ports())
    assert outcome.listings[0].acted == frozenset({GO.id})


def test_a_turn_with_no_dispatch_marks_nothing() -> None:
    world = World([])
    world.listing = TURN
    outcome = run_task(task(), world.ports())
    assert all(t.acted == frozenset() for t in outcome.listings)


def _line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, turn: ListingTurn) -> Any:  # JSON
    outcome = TaskOutcome(status="done", cause=None, dispatched=(), model_calls=1, strikes=(),
                          listings=(turn,))  # fmt: skip
    monkeypatch.setattr(cli, "run_task", lambda _task, _ports: outcome)
    run = Run(tmp_path)
    run()
    return run.result()["listings"][0]


def test_the_final_line_says_how_many_were_acted_on_and_whether_all_were_listed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _line(monkeypatch, tmp_path, dataclasses.replace(TURN, acted=frozenset({GO.id})))
    assert (row["acted"], row["acted_listed"]) == (1, True)


def test_an_acted_handle_the_turn_did_not_show_is_said(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _line(monkeypatch, tmp_path, dataclasses.replace(TURN, acted=frozenset({1987})))
    assert (row["acted"], row["acted_listed"]) == (1, False)


def _row(acted: int, listed: bool | None) -> dict[str, Any]:  # Any: JSON
    return {"eligible": 2000, "shown": 150, "cut": 1850, "keep_at_ceiling": False,
            "goal_at_subcap": False, "prompt_cut": 0, "prompt_tokens": 900,
            "acted": acted, "acted_listed": listed}  # fmt: skip


def _large(*listings: list[dict[str, Any]]) -> bool:
    runs = [_ok(LARGE, i + 1, final=_final(listings=rows)) for i, rows in enumerate(listings)]
    return accept([LARGE], runs, controls=None, runs_per_task=3, label="large").gates[
        "large_listed"
    ]


def test_large_runs_whose_acts_were_all_listed_pass_the_gate() -> None:
    ok = [_row(0, True), _row(1, True)]
    assert _large(ok, ok, ok) is True


def test_one_act_on_a_target_not_listed_fails_the_gate() -> None:
    ok = [_row(1, True)]
    assert _large(ok, ok, [_row(1, False)]) is False


def test_a_done_large_run_that_acted_on_nothing_fails_closed() -> None:
    ok = [_row(1, True)]
    assert _large(ok, ok, [_row(0, True)]) is False


@pytest.mark.parametrize("odd", [None, "yes", 1])
def test_an_unread_listed_fact_fails_closed(odd: object) -> None:
    ok = [_row(1, True)]
    bad = [{**_row(1, True), "acted_listed": odd}]
    assert _large(ok, ok, bad) is False


def test_a_set_without_large_tasks_is_not_gated() -> None:
    runs = [_ok(DONE, i + 1) for i in range(3)]
    gates = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").gates
    assert gates["large_listed"] is True
