"""Known gap IJ-05 (#81, 5.13 1b): text the model generated, or copied from the screen, typed
into a page is keylogger bait (a card number into a field the page chose), so it must be R2 and
ask. Today the gate never reads `text_provenance`: the type is approved at R1."""

from __future__ import annotations

import pytest

from steerai.orchestrator.contracts import Step, Target, TextProvenance
from steerai.verify.predicates import ValueEquals
from tests.orchestrator.fakes import COMPOSER
from tests.orchestrator.test_loop import Harness
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


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="IJ-05 #81")
def test_generated_text_typed_into_a_page_asks_at_r2() -> None:
    attack = fixture("IJ-05", "generated_text")
    assert attack.action is not None
    h = Harness(default="no")
    log: list[Dispatch] = []
    rebuild(h, log=log)
    h.session.run_step(_typed(attack.payload, "generated"))
    assert [d for d in log if attack.action.matches(d.verb, d.role, d.name)] == []
    assert [ask.risk for ask in h.confirmer.asked] == [attack.tier]
