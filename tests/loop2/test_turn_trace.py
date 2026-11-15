"""[V2.67] D: the redacted per-turn trace holds closed tokens and numbers only. Every new field is
fed page text, a window title and a file path, and none reaches a written record: the record
fails the check whole and is dropped, never written in part (CLAUDE.md: names, message text and
paths are redacted at the trace boundary)."""

from __future__ import annotations

import copy
import dataclasses
import json
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from steerai.loop2.decision import ActionDoc, Decision, SubgoalDoc
from steerai.loop2.history import (
    DELIVERIES,
    GRAMMAR,
    POSITIONAL_GRAMMAR,
    REFUSAL_TOKENS,
    VERBS,
    ClaimResult,
    HistoryRecord,
    LogEntry,
    Proven,
    render,
)
from steerai.loop2.loop import TokenUsage
from steerai.loop2.runner import run_task
from steerai.loop2.turn_trace import (
    KEY_TOKENS,
    UNKNOWN,
    CarrierFact,
    ParamProof,
    ProofAttempt,
    ReadFacts,
    TurnTrace,
    WatchFacts,
    closed,
    closed_proofs,
    positional,
    turn_record,
)
from tests.loop2.world_fakes import FIELD, GO, World, act, decision, report, task

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
    params=(
        ParamProof(0, (CarrierFact("url_param", True, True), CarrierFact("title", False, None))),
    ),
    content=True,
    content_by="title",
    guards=("owned_by_combobox", "option_evidence"),
    proven=True,
    proven_how="title",
)


def _into(records: list[dict[str, Any]]) -> Callable[[Mapping[str, object]], None]:
    """A trace port that keeps each record (Any: JSON, read back by the test)."""

    def sink(record: Mapping[str, object]) -> None:
        records.append(dict(record))

    return sink


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
                    {
                        "verb": "type",
                        "in_step": True,
                        "param": "param:0",
                        "text": True,
                        "key": None,
                    },
                    {
                        "verb": "press",
                        "in_step": True,
                        "param": None,
                        "text": False,
                        "key": "Enter",
                    },
                ],
                "claims": [
                    {"kind": "navigated", "in_step": True, "param": "param:0", "evidence": 1}
                ],
                "ask": False,
                "stop": None,
            },
            refusals=("id_not_listed", "press_not_at_focus"),
            watch=(WatchFacts("submit", 1.25),),
            tokens=TokenUsage(1200, 80),
            history=("stale_observation", "load_guard", "claim:s0:accepted", "proven:s1:claim"),
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
    ports = dataclasses.replace(world.ports(), turn_trace=_into(records))
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
    kept = run_task(task(), dataclasses.replace(world.ports(), turn_trace=_into(clean)))
    assert clean
    assert kept.turn_trace_dropped == 0

    real = turn_record

    def leaky(facts: TurnTrace) -> dict[str, Any]:  # Any: JSON
        return {**real(facts), "focus": TITLE}  # a title where a closed token belongs

    monkeypatch.setattr("steerai.loop2.runner.turn_record", leaky)
    world = World([decision(act("click", target=GO.id), first=True)])
    written: list[dict[str, Any]] = []  # Any: JSON
    outcome = run_task(task(), dataclasses.replace(world.ports(), turn_trace=_into(written)))
    assert written == []
    assert outcome.turn_trace_dropped == len(clean)


# ---- the ruling's (a): model-chosen names are written as positions ------------------------------

SUBGOAL_ID = "acme_q3_results"  # model text that passes the history grammar (lowercase, `_`)
PARAM_NAME = "account_name"
SUBGOAL_IDS = ("find_store", SUBGOAL_ID)
KNOWN = {SUBGOAL_ID: frozenset({PARAM_NAME, "quarter"}), "find_store": frozenset()}


def _entry(subgoal: str | None, param: str | None) -> LogEntry:
    return LogEntry(
        verb=sorted(VERBS)[0],
        handle=3,
        role="edit",
        subgoal=subgoal,
        param=param,
        delivery=sorted(DELIVERIES)[0],
        classes=(),
        tokens=(),
        read_back=None,
    )


RAW: dict[str, HistoryRecord] = {
    "act": _entry(SUBGOAL_ID, PARAM_NAME),
    "claim": ClaimResult(SUBGOAL_ID, "accepted"),
    "proven": Proven(SUBGOAL_ID, "claim"),
}


@pytest.mark.parametrize("kind", sorted(RAW))
def test_an_unsubstituted_history_line_fails_the_record(kind: str) -> None:
    raw = render(RAW[kind])
    assert GRAMMAR.fullmatch(raw)  # the history itself accepts the model's own id
    record = full_record()
    record["history"] = [raw]
    assert not closed(record)


@pytest.mark.parametrize("kind", sorted(RAW))
def test_a_positional_history_line_passes_and_names_nothing(kind: str) -> None:
    line = render(positional(RAW[kind], SUBGOAL_IDS, KNOWN))
    assert POSITIONAL_GRAMMAR.fullmatch(line)
    assert SUBGOAL_ID not in line
    assert PARAM_NAME not in line
    record = full_record()
    record["history"] = [line]
    assert closed(record)


def test_positions_follow_the_decision_shape_rule() -> None:
    """A subgoal by its place among the task's subgoals; a param by its place among its
    subgoal's declared params, sorted (`account_name` before `quarter`)."""
    entry = positional(_entry(SUBGOAL_ID, "quarter"), SUBGOAL_IDS, KNOWN)
    assert isinstance(entry, LogEntry)
    assert (entry.subgoal, entry.param) == ("s1", "param:1")
    assert positional(Proven("find_store", "claim"), SUBGOAL_IDS, KNOWN) == Proven("s0", "claim")


@pytest.mark.parametrize(
    "record",
    [_entry("not_a_subgoal", None), _entry(SUBGOAL_ID, "not_a_param"), Proven("gone", "claim")],
)
def test_a_name_not_found_is_never_written_raw(record: LogEntry | Proven) -> None:
    line = render(positional(record, SUBGOAL_IDS, KNOWN))
    assert UNKNOWN in line
    assert "not_a" not in line
    assert "gone" not in line
    trace = full_record()
    trace["history"] = [line]
    assert not closed(trace)


def test_the_runner_writes_no_model_chosen_subgoal_id_or_param_name() -> None:
    subgoals = [
        SubgoalDoc(id=SUBGOAL_ID, desc="search", params={PARAM_NAME: "red shoes"}, closes="page")
    ]
    typed = ActionDoc.model_validate(
        {"verb": "type", "subgoal": SUBGOAL_ID, "target": FIELD.id, "param": PARAM_NAME}
    )
    world = World([Decision(subgoals=subgoals, actions=[typed])], effects=[report("own_value")])
    records: list[dict[str, Any]] = []  # Any: JSON
    outcome = run_task(task(), dataclasses.replace(world.ports(), turn_trace=_into(records)))
    assert outcome.turn_trace_dropped == 0
    assert all(closed(r) for r in records)
    lines = [line for r in records for line in r["history"]]
    assert any(":edit:s0:param:0:" in line for line in lines), lines
    written = json.dumps(records)
    assert SUBGOAL_ID not in written
    assert PARAM_NAME not in written
