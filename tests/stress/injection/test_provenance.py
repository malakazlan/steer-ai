"""IJ-05 (#81, 5.13 1b [v2.27]): text the model generated, or copied from the screen, typed into
a page is keylogger bait (a card number into a field the page chose). The gate asks before every
type of text that is not verbatim in the person's request, showing it, at the step's own tier."""

from __future__ import annotations

import pytest

from steerai.orchestrator.contracts import Step, Target, TextProvenance
from steerai.verify.predicates import ValueEquals
from tests.orchestrator.fakes import COMPOSER
from tests.orchestrator.test_loop import Harness
from tests.orchestrator.test_takeover_loop import FakeMonitor, _wire
from tests.stress.injection.catalogue import fixture
from tests.stress.injection.hostile import Dispatch, rebuild


def _typed(text: str, provenance: TextProvenance) -> Step:
    return Step(
        id="t1",
        subgoal="g1",
        verb="type",
        target=Target(role="Edit", name="Message"),
        text=text,
        risk="R1",
        expect=ValueEquals(target_id=COMPOSER[0], text=text),
        text_provenance=provenance,
    )


def test_verbatim_text_from_the_goal_stays_r1() -> None:
    """The control: the person's own words are typed without an ask."""
    h = Harness(default="no")
    result = h.session.run_step(_typed("hello there", "verbatim"))
    assert result.ok is True, result.to_dict()
    assert h.confirmer.asked == []


def test_generated_text_typed_into_a_page_asks_first() -> None:
    attack = fixture("IJ-05", "generated_text")
    assert attack.action is not None
    h = Harness(default="no")
    log: list[Dispatch] = []
    rebuild(h, log=log)
    h.session.run_step(_typed(attack.payload, "generated"))
    assert [d for d in log if attack.action.matches(d.verb, d.role, d.name)] == []
    assert [(ask.risk, ask.reasons) for ask in h.confirmer.asked] == [
        (attack.tier, ("generated_text",))
    ]
    assert h.confirmer.asked[0].params["text"] == attack.payload, "the ask shows the text"


@pytest.mark.parametrize(("answer", "gate"), [("yes", "confirm"), ("no", "block")])
def test_the_ask_reason_is_traced_as_a_token(answer: str, gate: str) -> None:
    h = Harness(default=answer)
    result = h.session.run_step(_typed("my card number is 4111", "generated"))
    assert result.gate == gate
    assert result.evidence["ask_reasons"] == ["generated_text"]
    traced = [line for line in h.trace.lines if '"step_id": "t1"' in line]
    assert traced
    assert '"ask_reasons": ["generated_text"]' in traced[-1]
    assert all("4111" not in line for line in h.trace.lines), "the text never reaches the trace"


@pytest.mark.parametrize(
    ("provenance", "brackets"), [("generated", ["open", "closed"]), ("verbatim", [])]
)
def test_a_forced_ask_holds_the_monitor_open(
    provenance: TextProvenance, brackets: list[str]
) -> None:
    h = Harness(default="yes")
    monitor = FakeMonitor(h.clock, last_user=-100.0)
    _wire(h, monitor)
    h.session.run_step(_typed("hello there", provenance))
    assert monitor.asks == brackets


def test_text_labelled_verbatim_but_absent_from_the_goal_asks() -> None:
    """The gate recomputes provenance from the goal: a wrong label never fails open."""
    h = Harness(default="no")
    h.session._goal = "send hello there to Amir"
    h.session.run_step(_typed("my card number is 4111", "verbatim"))
    assert [ask.reasons for ask in h.confirmer.asked] == [("generated_text",)]


def test_verbatim_text_in_the_goal_stays_unasked_when_the_goal_is_known() -> None:
    h = Harness(default="no")
    h.session._goal = "send hello there to Amir"
    result = h.session.run_step(_typed("hello there", "verbatim"))
    assert result.ok is True, result.to_dict()
    assert h.confirmer.asked == []
