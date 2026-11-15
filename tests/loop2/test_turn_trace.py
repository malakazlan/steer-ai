"""[V2.67] D: the redacted per-turn trace holds closed tokens and numbers only. Every new field is
fed page text, a window title and a file path, and none reaches a written record: the record
fails the check whole and is dropped, never written in part (CLAUDE.md: names, message text and
paths are redacted at the trace boundary)."""

from __future__ import annotations

import copy
import dataclasses
import json
from collections.abc import Callable
from typing import Any

import pytest

from steerai.loop2 import runner
from steerai.loop2.history import REFUSAL_TOKENS
from steerai.loop2.loop import TokenUsage
from steerai.loop2.runner import run_task
from steerai.loop2.turn_trace import (
    KEY_TOKENS,
    CarrierFact,
    ParamProof,
    ProofAttempt,
    ReadFacts,
    TurnTrace,
    WatchFacts,
    closed,
    closed_proofs,
    turn_record,
)
from tests.loop2.world_fakes import GO, World, act, decision, task

PAGE_TEXT = "Quarterly results for the Acme account"
TITLE = "Acme Corp - Dashboard - Browser"
PATH = r"C:\Users\person\Documents\secret plan.txt"
FREE_TEXT = (PAGE_TEXT, TITLE, PATH)

ATTEMPT = ProofAttempt(
    subgoal=0,
    read_kind="after_action",
    read=ReadFacts(tier="full", stable=True, growing=False, young=False),
    skipped=None,
    gate="url",
    change=True,
    change_by=("navigated",),
    refused=(),
    params=(ParamProof(0, (CarrierFact("url_param", True, True), CarrierFact("title", False, None))),),
    content=True,
    content_by="title",
    guards=("owned_by_combobox", "option_evidence"),
    proven=True,
    proven_how="title",
)


def full_record() -> dict[str, Any]:  # Any: JSON
    return turn_record(
        TurnTrace(
            turn=2,
            listed=12,
            cut=3,
            growing=True,
            young=False,
            focus="elsewhere",
            decision={
                "turn": 2,
                "attempt": 0,
                "rejections": [],
                "subgoals": [{"closes": "page", "params": 1}],
                "actions": [
                    {"verb": "type", "in_step": True, "param": "param:0", "text": True, "key": None},
                    {"verb": "press", "in_step": True, "param": None, "text": False, "key": "Enter"},
                ],
                "claims": [{"kind": "navigated", "in_step": True, "param": "param:0", "evidence": 1}],
                "ask": False,
                "stop": None,
            },
            refusals=("id_not_listed", "press_not_at_focus"),
            watch=(WatchFacts("submit", 1.25),),
            tokens=TokenUsage(1200, 80),
            history=("stale_observation", "load_guard"),
            proofs=(ATTEMPT,),
        )
    )


def test_a_full_turn_record_of_closed_facts_passes() -> None:
    assert closed(full_record())
    assert closed_proofs((ATTEMPT,))


# every place in the record a value could be written, each fed free text
Setter = Callable[[dict[str, Any], str], None]  # Any: JSON


def _set(path: tuple[object, ...]) -> Setter:
    def setter(record: dict[str, Any], text: str) -> None:  # Any: JSON
        node: Any = record  # Any: a JSON node
        for step in path[:-1]:
            node = node[step]
        node[path[-1]] = text

    return setter


PLACES: dict[str, Setter] = {
    "focus": _set(("focus",)),
    "refusal": _set(("refusals", 0)),
    "history": _set(("history", 0)),
    "watch kind": _set(("watch", 0, "kind")),
    "decision verb": _set(("decision", "actions", 0, "verb")),
    "decision param": _set(("decision", "actions", 0, "param")),
    "decision key": _set(("decision", "actions", 1, "key")),
    "claim kind": _set(("decision", "claims", 0, "kind")),
    "subgoal closes": _set(("decision", "subgoals", 0, "closes")),
    "decision stop": _set(("decision", "stop")),
    "rejection": lambda r, t: r["decision"]["rejections"].append(t),
    "read kind": _set(("proofs", 0, "read_kind")),
    "tier": _set(("proofs", 0, "read", "tier")),
    "skipped": _set(("proofs", 0, "skipped")),
    "gate": _set(("proofs", 0, "gate")),
    "change by": _set(("proofs", 0, "change_by", 0)),
    "refused": lambda r, t: r["proofs"][0]["refused"].append(t),
    "carrier": _set(("proofs", 0, "params", 0, "carriers", 0, "carrier")),
    "content by": _set(("proofs", 0, "content_by")),
    "guard": _set(("proofs", 0, "guards", 0)),
    "proven how": _set(("proofs", 0, "proven_how")),
    "a new key": lambda r, t: r.__setitem__(t, 1),
    "a nested new key": lambda r, t: r["proofs"][0]["read"].__setitem__(t, True),
}


@pytest.mark.parametrize("text", FREE_TEXT)
@pytest.mark.parametrize("place", sorted(PLACES))
def test_free_text_anywhere_in_a_turn_record_fails_it_whole(place: str, text: str) -> None:
    record = copy.deepcopy(full_record())
    record["proofs"] = [json.loads(json.dumps(p)) for p in record["proofs"]]
    record["proofs"][0]["refused"] = list(record["proofs"][0]["refused"])
    PLACES[place](record, text)
    assert not closed(record)


@pytest.mark.parametrize("line", ["stale_observation", "load_guard"])
def test_a_history_line_is_only_ever_one_the_grammar_accepts(line: str) -> None:
    record = full_record()
    record["refusals"] = [line]  # a history line outside `history` is not a closed token
    assert not closed(record)


def test_the_refusal_allowlist_is_the_producers_own_closed_set() -> None:
    """Not a hand list: `history.REFUSAL_TOKENS`, the set every emitter is pinned to
    (`test_history_vocabulary`), and the one `_refuse` is typed by."""
    assert KEY_TOKENS["refusals"] is REFUSAL_TOKENS
    assert KEY_TOKENS["rejections"] is REFUSAL_TOKENS


@pytest.mark.parametrize("token", ["click", "title", "navigated", "turn"])
def test_a_closed_token_of_another_set_is_not_a_refusal(token: str) -> None:
    record = full_record()
    record["refusals"] = [token]
    assert not closed(record)


def test_a_number_that_is_not_finite_fails_the_record() -> None:
    record = full_record()
    record["watch"][0]["seconds"] = float("nan")
    assert not closed(record)


def test_a_proof_attempt_built_with_free_text_fails_its_check() -> None:
    """The types say closed; the check holds even when a caller ignores them."""
    for text in FREE_TEXT:
        bad = dataclasses.replace(ATTEMPT, guards=(text,))  # type: ignore[arg-type]  # a guard under test
        assert not closed_proofs((bad,))


# ---- the runner writes one closed record per turn -----------------------------------------------


def test_the_runner_writes_each_turn_closed_and_none_of_the_pages_text() -> None:
    world = World([decision(act("click", target=GO.id), first=True)])
    records: list[dict[str, Any]] = []  # Any: JSON
    ports = dataclasses.replace(world.ports(), turn_trace=records.append)
    run_task(task(), ports)
    assert records
    assert all(closed(r) for r in records)
    written = json.dumps(records)
    shown = {e.name for e in world.screen.snapshot.elements if e.name}
    shown.add(world.screen.snapshot.title)
    assert not [name for name in shown if name and name in written]
    assert records[0]["decision"]["actions"][0]["verb"] == "click"


def test_a_dropped_turn_record_is_counted_in_the_runs_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record that fails the check is never written, and the run's result says how many were
    dropped: a gap in the trace never reads as a clean run."""
    world = World([decision(act("click", target=GO.id), first=True)])
    clean: list[dict[str, Any]] = []  # Any: JSON
    kept = run_task(task(), dataclasses.replace(world.ports(), turn_trace=clean.append))
    assert clean
    assert kept.turn_trace_dropped == 0

    real = runner.turn_record

    def leaky(facts: TurnTrace) -> dict[str, Any]:  # Any: JSON
        return {**real(facts), "focus": TITLE}  # a title where a closed token belongs

    monkeypatch.setattr(runner, "turn_record", leaky)
    world = World([decision(act("click", target=GO.id), first=True)])
    written: list[dict[str, Any]] = []  # Any: JSON
    outcome = run_task(task(), dataclasses.replace(world.ports(), turn_trace=written.append))
    assert written == []
    assert outcome.turn_trace_dropped == len(clean)
