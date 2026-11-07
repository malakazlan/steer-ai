"""A2 the confirm ask (IJ-13, IJ-14): a send the request pre-approved still asks, and only the
exact `yes:<id>` of the open ask dispatches. A sloppy `y`, a bare `yes` on a non-interactive
console, a yes bound to another ask and silence never do. The console is the real
`ConsoleConfirmer` reading a typist's line; a bare `yes` counts only when a person is at an
interactive console (RN-09), which no page can be."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.console import ConsoleConfirmer
from steerai.orchestrator.contracts import Plan, Step, Subgoal
from steerai.orchestrator.gate import Ask
from steerai.orchestrator.plan_io import PlanError, plan_from_json, plan_to_json
from steerai.verify.predicates import Appeared
from tests.orchestrator.test_loop import CONVERSATION, HELLO, Harness, _send, _type
from tests.stress.injection.catalogue import SEND, Attack, fixture, pinned
from tests.stress.injection.hostile import Dispatch, Typist, rebuild

SENT = Appeared(role="Text", name_pattern=HELLO, within=CONVERSATION)
"""The send's effect in its file form (no snapshot-scoped anchor)."""


def _sends(log: list[Dispatch]) -> list[Dispatch]:
    return [d for d in log if SEND.matches(d.verb, d.role, d.name)]


def _preapproved() -> Step:
    return replace(_send(), preapproved_by_request=True)


def _file_plan(step: Step) -> Plan:
    return Plan(
        goal="send hello there to Amir",
        subgoals=(Subgoal(id="g1", desc="sent", condition=SENT),),
        steps=(step,),
    )


# ---- IJ-13: preapproval never skips the ask ---------------------------------------------------


def test_a_preapproved_send_still_asks_and_a_no_sends_nothing() -> None:
    attack = fixture("IJ-13", "preapproved_send")
    h = Harness(default="no")
    log: list[Dispatch] = []
    rebuild(h, log=log)
    h.session.run_step(_type())
    result = h.session.run_step(_preapproved())
    assert [ask.risk for ask in h.confirmer.asked] == [attack.tier]
    assert (result.gate, result.evidence["detail"]) == ("block", "user_declined")
    assert _sends(log) == []
    assert h.world.sent == []


def test_a_plan_files_preapproved_send_still_asks() -> None:
    plan = plan_from_json(plan_to_json(_file_plan(_preapproved())))
    assert plan.steps[0].preapproved_by_request is True
    h = Harness(default="no")
    h.session.run_step(_type())
    result = h.session.run_step(plan.steps[0])
    assert len(h.confirmer.asked) == 1
    assert result.gate == "block"
    assert h.world.sent == []


def test_a_plan_file_cannot_preapprove_generated_text() -> None:
    body = json.loads(plan_to_json(_file_plan(_preapproved())))
    body["steps"][0]["text_provenance"] = "generated"
    with pytest.raises(PlanError) as caught:
        plan_from_json(json.dumps(body))
    assert str(caught.value).startswith("$.steps[0]")


# ---- IJ-14: only the exact bound token ----------------------------------------------------------


def _console(line: str | None) -> tuple[ConsoleConfirmer, Typist]:
    screen = Typist(line)
    return ConsoleConfirmer(stdin=screen.stdin, stderr=screen, interactive=False), screen


@pytest.mark.parametrize("attack", pinned("IJ-14"), ids=lambda a: a.case)
def test_nothing_but_the_bound_yes_dispatches(attack: Attack) -> None:
    h = Harness()
    log: list[Dispatch] = []
    console, screen = _console(None if attack.case == "silence" else attack.payload)
    rebuild(h, confirmer=console, log=log)
    h.session.run_step(_type())
    result = h.session.run_step(_preapproved())
    assert screen.getvalue().count("ASK ") == 1, "the ask was shown"
    assert result.gate == attack.expected
    assert (result.code, result.evidence["detail"]) == (attack.code, attack.detail)
    assert _sends(log) == []
    assert h.world.sent == []
    assert "s3" not in h.ledger.records, "no write-ahead record without a yes"


def test_the_bound_yes_dispatches_exactly_once() -> None:
    h = Harness()
    log: list[Dispatch] = []
    console, _screen = _console("yes:{id}")
    rebuild(h, confirmer=console, log=log)
    h.session.run_step(_type())
    result = h.session.run_step(_preapproved())
    assert result.ok is True, result.to_dict()
    assert result.gate == "confirm"
    assert len(_sends(log)) == 1
    assert h.world.sent == [HELLO]


def test_a_yes_bound_to_an_earlier_ask_never_dispatches() -> None:
    """The answer to the first ask, replayed to the second: the id is the ask's, not the
    action's, so it is stale."""
    h = Harness()
    log: list[Dispatch] = []
    rebuild(h, log=log)
    first: list[str] = []

    def replay(ask: Ask) -> None:
        if not first:
            first.append(ask.decision_id)
            h.confirmer.answers.append("no")
        else:
            h.confirmer.answers.append(f"yes:{first[0]}")

    h.confirmer.on_ask = replay
    h.session.run_step(_type())
    assert h.session.run_step(_preapproved()).gate == "block"
    again = h.session.run_step(_preapproved())
    assert (again.gate, again.code) == ("pending", ErrorCode.GATE_PENDING_USER)
    assert len(h.confirmer.asked) == 2
    assert _sends(log) == []
    assert h.world.sent == []
