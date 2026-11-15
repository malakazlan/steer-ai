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

from steerai.action.dispositions import ActionResult, Delivery
from steerai.action.execution import REFUSAL_DETAILS
from steerai.errors import ErrorCode
from steerai.loop2.controller import ControllerError, DecideRecord
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
from steerai.loop2.listing import ListingTurn
from steerai.loop2.loop import PORT_DETAILS, ActResult, TokenUsage
from steerai.loop2.runner import run_task
from steerai.loop2.turn_trace import (
    ACT_DETAILS,
    KEY_TOKENS,
    REPAIR_TOKENS,
    UNKNOWN,
    ActFact,
    CarrierFact,
    ParamProof,
    ProofAttempt,
    ReadFacts,
    RefusalFact,
    TurnTrace,
    WatchFacts,
    closed,
    closed_proofs,
    detail_token,
    positional,
    turn_record,
)
from tests.loop2.world_fakes import (
    FIELD,
    GO,
    PAGE,
    URL,
    World,
    act,
    decision,
    element,
    observed,
    report,
    task,
)

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
                        "commit": "none",
                        "risk": "R1",
                        "why": "state_floor",
                    },
                    {
                        "verb": "press",
                        "in_step": True,
                        "param": None,
                        "text": False,
                        "key": "Enter",
                        "commit": "submit",
                        "risk": None,
                        "why": None,
                    },
                ],
                "claims": [
                    {"kind": "navigated", "in_step": True, "param": "param:0", "evidence": 1}
                ],
                "ask": False,
                "stop": None,
            },
            refusals=(
                RefusalFact("id_not_listed", None),
                RefusalFact("not_delivered", "no_options"),
            ),
            acts=(ActFact("type", "delivered_verified"), ActFact("press", "not_sent")),
            repair=True,
            repair_tokens=("schema", "id_not_listed"),
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
    "refusal": _set(("refusals", 0, "refusal")),
    "refusal detail": _set(("refusals", 1, "detail")),
    "act verb": _set(("acts", 0, "verb")),
    "act result": _set(("acts", 1, "result")),
    "repair token": lambda r, t: r["repair_tokens"].append(t),
    "history": _set(("history", 0)),
    "watch kind": _set(("watch", 0, "kind")),
    "decision verb": _set(("decision", "actions", 0, "verb")),
    "decision param": _set(("decision", "actions", 0, "param")),
    "decision key": _set(("decision", "actions", 1, "key")),
    "claim kind": _set(("decision", "claims", 0, "kind")),
    "subgoal closes": _set(("decision", "subgoals", 0, "closes")),
    "action commit": _set(("decision", "actions", 1, "commit")),
    "action risk": _set(("decision", "actions", 0, "risk")),
    "action why": _set(("decision", "actions", 0, "why")),
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
    record["refusals"][0]["refusal"] = line  # a history line outside `history` is no token
    assert not closed(record)


def test_the_refusal_allowlist_is_the_producers_own_closed_set() -> None:
    """Not a hand list: `history.REFUSAL_TOKENS`, the set every emitter is pinned to
    (`test_history_vocabulary`), and the one `_refuse` is typed by."""
    assert KEY_TOKENS["refusal"] is REFUSAL_TOKENS
    assert KEY_TOKENS["rejections"] is REFUSAL_TOKENS


def test_the_detail_allowlist_is_the_executors_and_the_ports_own_closed_sets() -> None:
    """Not a hand list: the sets their producers are pinned to (`test_refusal_details`)."""
    assert KEY_TOKENS["detail"] is ACT_DETAILS
    assert REFUSAL_DETAILS | PORT_DETAILS | {"unlisted"} == ACT_DETAILS
    assert KEY_TOKENS["repair_tokens"] is REPAIR_TOKENS
    assert {"not_json", "schema", "id_not_listed", "param_unknown"} <= REPAIR_TOKENS


@pytest.mark.parametrize(
    ("key", "token"),
    [("commit", "R2"), ("commit", "click"), ("risk", "R4"), ("risk", "submit"),
     ("why", "commit_not_in_step"), ("why", "R2")],
)  # fmt: skip
def test_an_actions_commit_risk_and_rule_are_tokens_of_their_own_sets(key: str, token: str) -> None:
    record = full_record()
    record["decision"]["actions"][0][key] = token
    assert not closed(record)


@pytest.mark.parametrize("token", ["click", "title", "navigated", "turn"])
def test_a_closed_token_of_another_set_is_not_a_refusal(token: str) -> None:
    record = full_record()
    record["refusals"][0]["refusal"] = token
    assert not closed(record)


@pytest.mark.parametrize(
    ("key", "token"),
    [("detail", "click"), ("detail", "navigated"), ("result", "no_options"), ("result", "click")],
)
def test_a_detail_or_a_result_is_only_a_token_of_its_own_set(key: str, token: str) -> None:
    record = full_record()
    record["refusals" if key == "detail" else "acts"][1][key] = token
    assert not closed(record)


def test_a_detail_outside_the_closed_sets_is_written_as_unlisted() -> None:
    assert detail_token("no_options") == "no_options"
    assert detail_token("killed") == "killed"
    assert detail_token(None) is None
    for text in (*FREE_TEXT, 3, b"no_options"):
        assert detail_token(text) == "unlisted"


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


# ---- the gallery diagnosis: each refusal's detail, the batch's acts, the repair round --------


def _not_delivered(detail: str) -> ActResult:
    refused = ActionResult(
        "click", Delivery.NOT_DELIVERED, code=ErrorCode.ELEMENT_NOT_ACTIONABLE,
        evidence={"detail": detail},
    )  # fmt: skip
    return ActResult(refused, None)


@pytest.mark.parametrize(("detail", "written"), [("no_options", "no_options"), (TITLE, "unlisted")])
def test_a_not_delivered_refusal_carries_its_closed_detail(detail: str, written: str) -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True)], results=[_not_delivered(detail)]
    )
    records: list[dict[str, Any]] = []  # Any: JSON
    outcome = run_task(task(), dataclasses.replace(world.ports(), turn_trace=_into(records)))
    assert outcome.turn_trace_dropped == 0
    first = records[0]
    assert {"refusal": "not_delivered", "detail": written} in first["refusals"]
    assert first["acts"] == [{"verb": "click", "result": "not_delivered"}]
    assert TITLE not in json.dumps(records)


def test_the_batch_is_written_as_its_verbs_in_order_with_each_result() -> None:
    focus = act("focus", target=FIELD.id)
    typed = act("type", target=FIELD.id, param="text")
    world = World([decision(focus, typed, first=True)], effects=[report("own_value")])
    records: list[dict[str, Any]] = []  # Any: JSON
    run_task(task(), dataclasses.replace(world.ports(), turn_trace=_into(records)))
    verbs = [a["verb"] for a in records[0]["acts"]]
    assert verbs == ["focus", "type"][: len(verbs)]
    assert verbs
    assert all(closed(r) for r in records)


def test_an_act_refused_before_the_port_is_not_sent() -> None:
    world = World([decision(act("focus", target=GO.id), first=True)])  # a focus on a button
    records: list[dict[str, Any]] = []  # Any: JSON
    run_task(task(), dataclasses.replace(world.ports(), turn_trace=_into(records)))
    assert records[0]["acts"] == [{"verb": "focus", "result": "not_sent"}]
    assert records[0]["refusals"] == [{"refusal": "focus_needs_field", "detail": None}]
    assert world.dispatched == []


def test_a_repaired_decision_is_written_with_its_rejection_tokens() -> None:
    world = World([decision(act("click", target=GO.id), first=True)])
    ports = world.ports()
    real = ports.decide

    def repaired(view: Any) -> Any:  # Any: the port's own TurnView and Decided
        return dataclasses.replace(real(view), calls=2, rejections=("schema",))

    records: list[dict[str, Any]] = []  # Any: JSON
    run_task(task(), dataclasses.replace(ports, decide=repaired, turn_trace=_into(records)))
    assert (records[0]["repair"], records[0]["repair_tokens"]) == (True, ["schema"])


def test_a_turn_rejected_after_its_repair_is_traced_with_both_attempts() -> None:
    """The 3d `port_rejected` runs: the failed turn is written, with its repair round."""
    failed = ControllerError(
        "rejected", ("id_not_listed",),
        record=DecideRecord("v", 2, True, True, rejections=("schema", "id_not_listed")),
    )  # fmt: skip
    world = World([decision(act("wait"), first=True)])
    world.decide_raises = failed
    records: list[dict[str, Any]] = []  # Any: JSON
    outcome = run_task(task(), dataclasses.replace(world.ports(), turn_trace=_into(records)))
    assert outcome.reason == "port_rejected"
    assert len(records) == 1
    assert (records[0]["repair"], records[0]["repair_tokens"]) == (
        True,
        ["schema", "id_not_listed"],
    )
    assert records[0]["decision"] is None
    assert records[0]["turn"] == 1


def test_the_decide_port_hands_the_repair_round_to_the_runner() -> None:
    from steerai.loop2.decide_port import DecideCalls, Reply, TaskGenerations  # noqa: PLC0415
    from steerai.loop2.prompt_inputs import make_decide  # noqa: PLC0415
    from tests.loop2.test_listing_wiring import GOAL, _view  # noqa: PLC0415

    sub = {"id": "s1", "desc": "x", "params": {"text": "red shoes"}, "closes": "page"}
    replies = [
        {"subgoals": [sub], "actions": [{"verb": "click", "target": 999_999, "subgoal": "s1"}]},
        {"subgoals": [sub], "actions": [{"verb": "click", "target": GO.id, "subgoal": "s1"}]},
    ]

    def provider(_system: list[str], _blocks: list[str]) -> Reply:
        return Reply(json.dumps(replies.pop(0)), "end_turn", TokenUsage(10, 5))

    calls = DecideCalls(provider, interrupted=lambda: None, generations=TaskGenerations(),
                        late_error=lambda _t: None)  # fmt: skip
    decided = make_decide(calls, goal=GOAL, task_fence="taskfence01")(_view())
    assert (decided.calls, decided.rejections) == (2, ("id_not_listed",))


# ---- lg1: each action's declared commit, its rated tier and the rule that set it ---------------


def _first_actions(world: World) -> list[dict[str, Any]]:  # Any: JSON
    records: list[dict[str, Any]] = []  # Any: JSON
    outcome = run_task(task(), dataclasses.replace(world.ports(), turn_trace=_into(records)))
    assert outcome.turn_trace_dropped == 0
    assert all(closed(r) for r in records)
    actions: list[dict[str, Any]] = records[0]["decision"]["actions"]  # Any: JSON
    return actions


def test_a_declared_commit_blocked_at_r2_is_traced_with_its_rule() -> None:
    """lg1: blocked `commit_not_in_step` with nothing dispatched; the turn says why."""
    world = World([decision(act("click", target=GO.id, commit="submit"), first=True)])
    (action,) = _first_actions(world)
    assert world.dispatched == []
    assert (action["commit"], action["risk"], action["why"]) == ("submit", "R2", "commit_declared")


def test_a_plain_click_is_r0_with_no_rule() -> None:
    (action,) = _first_actions(World([decision(act("click", target=GO.id), first=True)]))
    assert (action["commit"], action["risk"], action["why"]) == ("none", "R0", None)


def test_a_deny_word_is_traced_as_the_pattern_never_as_the_word() -> None:
    delete = element(11, "Button", "Delete account forever", (9, 11), 1)
    world = World(
        [decision(act("click", target=delete.id), first=True)], screen=observed([*PAGE, delete])
    )
    records: list[dict[str, Any]] = []  # Any: JSON
    run_task(task(), dataclasses.replace(world.ports(), turn_trace=_into(records)))
    (action,) = records[0]["decision"]["actions"]
    assert action["why"] == "deny_pattern"
    assert action["risk"] in {"R2", "R3"}
    assert "Delete" not in json.dumps(records)


def test_an_action_the_batch_never_rated_has_no_tier() -> None:
    first = act("click", target=GO.id, commit="submit")  # ends the task at R2
    world = World([decision(first, act("click", target=GO.id), first=True)])
    actions = _first_actions(world)
    assert [a["risk"] for a in actions] == ["R2", None]


# ---- #435 review: facts of this turn only; one record per turn on every path --------------------

SEEN_LISTING = ListingTurn(eligible=40, shown=30, cut=10, keep_at_ceiling=False,
                           goal_at_subcap=False, prompt_cut=0, prompt_tokens=900,
                           listed=frozenset({GO.id}))  # fmt: skip


def _records(world: World, ports: Any = None) -> list[dict[str, Any]]:  # Any: Ports, JSON
    records: list[dict[str, Any]] = []  # Any: JSON
    run_task(task(), dataclasses.replace(ports or world.ports(), turn_trace=_into(records)))
    assert all(closed(r) for r in records)
    return records


def test_a_failed_decide_writes_its_listing_counts_as_unknown() -> None:
    spent = DecideRecord("v", 2, True, True, rejections=("schema",))
    failed = ControllerError("rejected", ("id_not_listed",), record=spent)
    world = World([decision(act("wait"), first=True)])
    world.decide_raises = failed
    (record,) = _records(world)
    assert (record["listed"], record["cut"]) == (None, None)


def test_a_turn_whose_decide_reports_no_listing_never_takes_an_earlier_one() -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True), Decision(stop="cannot")],
        effects=[report("content_changed")],
    )
    ports = world.ports()
    real = ports.decide
    shown = iter([SEEN_LISTING, None])

    def decide(view: Any) -> Any:  # Any: the port's own TurnView and Decided
        return dataclasses.replace(real(view), listing=next(shown))

    first, second = _records(world, dataclasses.replace(ports, decide=decide))
    assert (first["listed"], first["cut"]) == (30, 10)
    assert (second["listed"], second["cut"]) == (None, None)


def test_an_audited_done_tasks_last_record_is_numbered_as_its_decision() -> None:
    from tests.loop2.test_final_audit import ALLOW, _value_only  # noqa: PLC0415

    records = _records(_value_only(ALLOW))
    assert [r["turn"] for r in records] == [1, 2]
    assert all(r["turn"] == r["decision"]["turn"] for r in records)


def test_a_turn_discarded_as_stale_after_its_decide_is_written_once() -> None:
    moved = dataclasses.replace(observed(), url=URL + "next", document_url=URL + "next")
    world = World(
        [decision(act("click", target=GO.id), first=True), Decision(stop="cannot")],
        observations=[observed(), moved],
    )
    records = _records(world)
    assert world.dispatched == []
    assert [r["turn"] for r in records] == [1, 2]


def test_a_turn_whose_target_read_finds_the_page_moved_is_written_once() -> None:
    from steerai.orchestrator.loop import REUSE_FRESHNESS_S  # noqa: PLC0415

    moved = dataclasses.replace(observed(), url=URL + "next", document_url=URL + "next")
    world = World(
        [decision(act("click", target=GO.id), first=True)],
        observations=[observed(), observed(), moved],
    )
    world.observe_lag_s = REUSE_FRESHNESS_S + 1.0
    records = _records(world)
    assert world.dispatched == []
    assert [r["turn"] for r in records] == [1, 2]


def test_a_turn_traced_before_the_task_ends_at_the_loop_top_is_not_written_again() -> None:
    """A takeover after the turn's one act ends the task at the next loop top: the turn was
    already written when it ended, and the end writes nothing more."""
    world = World(
        [decision(act("click", target=GO.id), first=True)],
        effects=[report("content_changed")],
        interrupt_after=1,
    )
    records = _records(world)
    assert len(world.dispatched) == 1
    assert [r["turn"] for r in records] == [1]
