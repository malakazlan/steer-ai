"""Stage 2 plan §6 (rev 4.4), the parts that do not wait on the lead: the run facts the final
line carries and the stage summary aggregates. Closed tokens and numbers only.
- per run: the full-read cost per observation as a summary, the strikes by reason, the stale
  observations, the unread automation ids, and every observation's URL read by cause;
- per listing turn ([V2.53]): the root chain's rows and the prompt's bytes, beside the counts
  #359/#367 already emit."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from steerai.loop2 import cli
from steerai.loop2.controller import prompt_bytes
from steerai.loop2.decide_port import DecideCalls, Reply, TaskGenerations
from steerai.loop2.decision import Decision
from steerai.loop2.listing import ListingTurn, select_listing
from steerai.loop2.loop import URL_READS, TaskOutcome, TokenUsage, url_read
from steerai.loop2.prompt_inputs import make_decide
from steerai.loop2.runner import run_task
from steerai.loop2.stage1 import accept
from steerai.world.reads import ReadCost
from tests.loop2.test_cli_v2 import Run
from tests.loop2.test_listing_cap import _rooted
from tests.loop2.test_listing_wiring import GOAL, _view
from tests.loop2.test_stage1 import DONE, _final, _ok
from tests.loop2.world_fakes import URL, World, observed, task

# ---- the URL read by cause, per observation taken in ---------------------------------------------


@pytest.mark.parametrize(
    ("changes", "cause"),
    [
        ({}, "read"),
        ({"url": None, "url_observed": False, "document_url": None}, "no_bar"),
        ({"url": None, "document_url": None}, "document_url_unread"),
        ({"url": None}, "not_composed"),
    ],
)
def test_an_observations_url_read_has_one_closed_cause(changes: dict[str, Any], cause: str) -> None:
    assert url_read(dataclasses.replace(observed(), **changes)) == cause


def test_the_causes_are_closed_and_ordered() -> None:
    assert URL_READS == ("read", "no_bar", "document_url_unread", "not_composed")


def test_every_observation_taken_in_is_counted_by_its_url_cause() -> None:
    """As the unread automation ids (#301): each observation passes the stamp port once."""
    blind = dataclasses.replace(observed(), url=None, document_url=None)
    world = World([Decision(stop="cannot")], screen=blind)
    outcome = run_task(task(), world.ports())
    stamps = world.events.count("stamp")
    assert stamps >= 2
    assert dict(outcome.url_reads) == {"read": 0, "no_bar": 0, "document_url_unread": stamps,
                                       "not_composed": 0}  # fmt: skip


def test_a_read_url_counts_as_read() -> None:
    world = World([Decision(stop="cannot")])
    outcome = run_task(task(), world.ports())
    assert dict(outcome.url_reads)["read"] == world.events.count("stamp")
    assert URL  # the fake's own URL is what was read


# ---- the listing turn: the root chain and the prompt's bytes -------------------------------------


@pytest.mark.parametrize("depth", [0, 1, 20, 60])
def test_the_listing_says_how_many_rows_the_root_chain_has(depth: int) -> None:
    shown, root = _rooted(depth)
    assert select_listing(shown).root_depth == len(root) == depth + 1


def test_with_no_single_top_document_the_root_chain_is_unread() -> None:
    from tests.loop2.world_fakes import GO, snapshot  # noqa: PLC0415 - a page with no Document

    assert select_listing(snapshot([GO])).root_depth is None


def test_the_turn_carries_the_root_chain_and_the_bytes_of_the_prompt_sent() -> None:
    sent: list[int] = []

    class Provider:
        def __call__(self, system: list[str], blocks: list[str]) -> Reply:
            sent.append(prompt_bytes(system, blocks))
            return Reply(json.dumps({"stop": "cannot"}), "end_turn", TokenUsage(1234, 5))

    calls = DecideCalls(Provider(), interrupted=lambda: None, generations=TaskGenerations(),
                        late_error=lambda _t: None)  # fmt: skip
    decided = make_decide(calls, goal=GOAL, task_fence="taskfence01")(_view())
    assert decided.listing is not None
    assert decided.listing.prompt_bytes == sent[0] > 0
    assert decided.listing.root_depth is not None


# ---- the final line ------------------------------------------------------------------------------


TURN = ListingTurn(
    eligible=10,
    shown=8,
    cut=2,
    keep_at_ceiling=False,
    goal_at_subcap=True,
    prompt_cut=0,
    prompt_tokens=900,
    listed=frozenset(),
    root_depth=7,
    prompt_bytes=4000,
)
OUTCOME = TaskOutcome(
    status="done",
    cause=None,
    dispatched=(),
    model_calls=3,
    strikes=(("effect_none",), ("effect_none", "refused")),
    tokens=21.0,
    cost_usd=0.0,
    wall_s=1.0,
    read_costs=(
        ReadCost(full_reads=1, full_ms=100),
        ReadCost(full_reads=2, full_ms=300),
        ReadCost(poll_reads=4, poll_ms=20),  # no full read: not a full-read observation
        ReadCost(full_reads=1, full_ms=900),
    ),
    stale_observations=3,
    unread_automation_ids=4,
    url_reads=(("read", 5), ("no_bar", 0), ("document_url_unread", 2), ("not_composed", 1)),
    listings=(TURN,),
)


def _line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: TaskOutcome) -> Any:  # JSON
    monkeypatch.setattr(cli, "run_task", lambda _task, _ports: outcome)
    run = Run(tmp_path)
    run()
    return run.result()


def test_the_final_line_summarises_the_full_read_cost_per_observation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Not a list (the line stays small): per observation with a full read, its milliseconds
    per full read (100, 150, 900)."""
    line = _line(monkeypatch, tmp_path, OUTCOME)
    assert line["read_cost"] == {"observations": 3, "full_reads": 4, "p50_ms": 150.0,
                                 "p95_ms": 900.0, "max_ms": 900.0}  # fmt: skip


def test_the_final_line_counts_the_strikes_by_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    line = _line(monkeypatch, tmp_path, OUTCOME)
    assert line["strikes"] == {"count": 2, "by_reason": {"effect_none": 2, "refused": 1}}


def test_the_final_line_carries_the_observation_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    line = _line(monkeypatch, tmp_path, OUTCOME)
    assert (line["stale_observations"], line["unread_automation_ids"]) == (3, 4)
    assert line["url_reads"] == {"read": 5, "no_bar": 0, "document_url_unread": 2,
                                 "not_composed": 1}  # fmt: skip


def test_each_listing_row_adds_the_root_chain_and_the_prompt_bytes_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#359/#367's counts stay as they are; the two new ones join them, nothing duplicated."""
    row = _line(monkeypatch, tmp_path, OUTCOME)["listings"][0]
    assert row == {"eligible": 10, "shown": 8, "cut": 2, "keep_at_ceiling": False,
                   "goal_at_subcap": True, "prompt_cut": 0, "prompt_tokens": 900,
                   "root_depth": 7, "prompt_bytes": 4000}  # fmt: skip


def test_a_run_with_no_full_read_summarises_to_nulls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bare = dataclasses.replace(OUTCOME, read_costs=())
    assert _line(monkeypatch, tmp_path, bare)["read_cost"] == {
        "observations": 0, "full_reads": 0, "p50_ms": None, "p95_ms": None, "max_ms": None,
    }  # fmt: skip


def test_the_unfinished_line_says_every_new_fact_is_unknown() -> None:
    from steerai.loop2.task import Budgets  # noqa: PLC0415

    shape = cli._unfinished(Budgets(max_actions=1, max_model_calls=1, max_tokens=1,
                                    max_cost_usd=1.0, max_wall_s=1.0))  # fmt: skip
    for key in ("read_cost", "strikes", "stale_observations", "unread_automation_ids",
                "url_reads"):  # fmt: skip
        assert shape[key] is None, key


# ---- the stage summary ---------------------------------------------------------------------------


def _facts(**changes: Any) -> dict[str, Any]:  # Any: JSON
    base: dict[str, Any] = {
        "read_cost": {"observations": 3, "full_reads": 4, "p50_ms": 150.0, "p95_ms": 900.0,
                      "max_ms": 900.0},
        "strikes": {"count": 2, "by_reason": {"effect_none": 2, "refused": 1}},
        "stale_observations": 3,
        "unread_automation_ids": 4,
        "url_reads": {"read": 5, "no_bar": 0, "document_url_unread": 2, "not_composed": 1},
        "listings": [{"eligible": 10, "shown": 8, "cut": 2, "keep_at_ceiling": False,
                      "goal_at_subcap": True, "prompt_cut": 0, "prompt_tokens": 900,
                      "root_depth": 7, "prompt_bytes": 4000}],
    }  # fmt: skip
    return {**base, **changes}


def _report(*finals: dict[str, Any]) -> dict[str, Any]:  # Any: JSON
    runs = [_ok(DONE, i + 1, final=_final(**f)) for i, f in enumerate(finals)]
    report = accept([DONE], runs, controls=(5, 5), runs_per_task=3, label="t").report
    json.dumps(report, allow_nan=False)
    return report


def test_the_summary_sums_the_counts_over_the_runs() -> None:
    report = _report(_facts(), _facts(), _facts())
    assert report["strikes"] == {"count": 6, "by_reason": {"effect_none": 6, "refused": 3}}
    assert (report["stale_observations"], report["unread_automation_ids"]) == (9, 12)
    assert report["url_reads"] == {"read": 15, "no_bar": 0, "document_url_unread": 6,
                                   "not_composed": 3}  # fmt: skip
    assert report["run_facts_incomplete"] == 0


def test_the_summary_combines_the_read_cost_summaries_without_pretending_a_pooled_p95() -> None:
    slow = _facts(read_cost={"observations": 2, "full_reads": 2, "p50_ms": 400.0,
                             "p95_ms": 2000.0, "max_ms": 2040.0})  # fmt: skip
    report = _report(_facts(), _facts(), slow)
    assert report["read_cost"] == {"observations": 8, "full_reads": 10, "run_p50_ms_p50": 150.0,
                                   "run_p95_ms_max": 2000.0, "max_ms": 2040.0}  # fmt: skip


def test_the_summary_adds_the_listing_facts() -> None:
    deep = _facts(listings=[{**_facts()["listings"][0], "root_depth": 30, "prompt_bytes": 9000}])
    listing = _report(_facts(), _facts(), deep)["listing"]
    assert listing["goal_at_subcap"] == 3
    assert (listing["root_depth_max"], listing["root_depth_unread"]) == (30, 0)
    assert (listing["prompt_bytes_p95"], listing["prompt_bytes_max"]) == (9000.0, 9000)


def test_an_odd_or_unknown_value_is_said_never_summed() -> None:
    """As the watch deadlines: a count that is not a whole number, or a cause or reason that is
    not a closed token, is counted incomplete; a run with no facts at all (the unfinished
    line's nulls) is skipped."""
    odd = _facts(
        strikes={"count": "x", "by_reason": {"effect_none": 1, "made_up": 2}},
        stale_observations=float("nan"),
        url_reads={"read": 1, "elsewhere": 3},
    )
    unknown = _facts(read_cost=None, strikes=None, stale_observations=None,
                     unread_automation_ids=None, url_reads=None)  # fmt: skip
    report = _report(_facts(), odd, unknown)
    assert report["run_facts_incomplete"] == 4  # count, made_up, the NaN, elsewhere
    assert report["strikes"]["by_reason"] == {"effect_none": 3, "refused": 1}
    assert report["url_reads"]["read"] == 6


def test_a_listing_without_a_root_chain_is_counted_unread() -> None:
    flat = _facts(listings=[{**_facts()["listings"][0], "root_depth": None}])
    assert _report(_facts(), flat, _facts())["listing"]["root_depth_unread"] == 1
