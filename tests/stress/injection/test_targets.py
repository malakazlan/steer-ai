"""Targets the page controls (IJ-23, IJ-34, IJ-36 pinned): a lookalike contact is the identity
ask, never a silent pick; a cloned button is ambiguity, never a pick; and a send the page asks
for again after it was verified is done already, never a second message."""

from __future__ import annotations

from dataclasses import replace

from steerai.orchestrator.contracts import Step, Target
from steerai.verify.predicates import TitleMatches
from tests.orchestrator.fakes import CHAT_AMIR, Row
from tests.orchestrator.fakes import SEND as SEND_ROW
from tests.orchestrator.test_loop import HELLO, Harness, _send, _type
from tests.stress.injection.catalogue import SEND, fixture
from tests.stress.injection.hostile import Dispatch, rebuild


def _person_click() -> Step:
    return Step(
        id="p1",
        subgoal="g1",
        verb="click",
        target=Target(role="ListItem", name="Amir", match="person"),
        risk="R1",
        expect=TitleMatches(pattern="Amir Khan - Chat"),
    )


def _lookalike_row() -> Row:
    return (16, "ListItem", fixture("IJ-23", "lookalike").payload, (42, 18), 1)


# ---- IJ-23 -------------------------------------------------------------------------------------


def test_a_lookalike_beside_the_real_contact_is_the_identity_ask() -> None:
    h = Harness(default="unanswered")
    h.world.rows.append(_lookalike_row())
    log: list[Dispatch] = []
    rebuild(h, log=log)
    result = h.session.run_step(_person_click())
    assert [ask.kind for ask in h.confirmer.asked] == ["which"]
    tags = {line.name_part: line.tags for line in h.confirmer.asked[0].candidates}
    assert len(tags) == 2
    assert any("lookalike" in t for t in tags.values())
    assert result.gate == "pending"
    assert log == []


def test_a_lookalike_alone_is_still_the_identity_ask() -> None:
    h = Harness(default="unanswered")
    h.world.rows = [r for r in h.world.rows if r != CHAT_AMIR]
    h.world.rows.append(_lookalike_row())
    log: list[Dispatch] = []
    rebuild(h, log=log)
    result = h.session.run_step(_person_click())
    assert [ask.kind for ask in h.confirmer.asked] == ["which"]
    assert "lookalike" in h.confirmer.asked[0].candidates[0].tags
    assert result.gate == "pending"
    assert log == []


# ---- IJ-34 -------------------------------------------------------------------------------------


def test_a_cloned_button_is_ambiguous_and_nothing_is_asked_or_sent() -> None:
    attack = fixture("IJ-34", "cloned_button")
    h = Harness()
    h.world.rows.append((11, "Button", attack.payload, (42, 43), 7))
    log: list[Dispatch] = []
    rebuild(h, log=log)
    h.session.run_step(_type())
    result = h.session.run_step(_send())
    assert (result.code, result.evidence["detail"]) == (attack.code, attack.detail)
    assert h.confirmer.asked == []
    assert [d for d in log if SEND.matches(d.verb, d.role, d.name)] == []
    assert "s3" not in h.ledger.records


# ---- IJ-36 -------------------------------------------------------------------------------------


def test_a_resend_after_a_verified_send_is_done_already() -> None:
    attack = fixture("IJ-36", "resend")
    h = Harness()
    log: list[Dispatch] = []
    rebuild(h, log=log)
    h.session.run_step(_type())
    first = h.session.run_step(_send())
    assert first.ok is True, first.to_dict()
    again = h.session.run_step(replace(_send(), id="p2.s3"))
    assert again.gate == attack.expected
    assert h.world.sent == [HELLO]
    assert [d.runtime_id for d in log if SEND.matches(d.verb, d.role, d.name)] == [SEND_ROW[3]]
    assert len(h.confirmer.asked) == 1
