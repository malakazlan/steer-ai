"""Deny words (5.13 1a; IJ-15 pinned; IJ-01 #81, IJ-03 #83 and IJ-04 #84 known gaps): a page
that names a control to slip past the deny matcher still has the step raised, so the gate asks
or blocks and nothing is dispatched without the person's yes."""

from __future__ import annotations

import pytest

from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.gate import effective_risk
from steerai.verify.predicates import TitleMatches
from tests.orchestrator.test_loop import Harness
from tests.stress.injection.catalogue import Attack, fixture, pinned
from tests.stress.injection.hostile import Dispatch, HostilePage, rebuild

UNCHANGED = TitleMatches(pattern="Amir Khan - Chat")


def _click(attack: Attack, *, automation_id: str = "") -> Step:
    assert attack.action is not None
    return Step(
        id="c1",
        subgoal="g1",
        verb="click",
        target=Target(role="Button", name=attack.action.name, automation_id=automation_id),
        risk="R0",
        expect=UNCHANGED,
    )


def _plan_id(attack: Attack) -> str:
    return attack.payload if attack.vector == "automation_id" else ""


def _on_page(
    attack: Attack, *, automation_ids: dict[str, str] | None = None
) -> tuple[Harness, list[Dispatch]]:
    """The attack's button on the chat page; the person answers no to any ask."""
    assert attack.action is not None
    h = Harness(default="no")
    h.world.rows.append((11, "Button", attack.action.name, (42, 43), 7))
    log: list[Dispatch] = []
    rebuild(h, log=log, observer=HostilePage(h.world, automation_ids or {}))
    return h, log


def _dispatched(attack: Attack, log: list[Dispatch]) -> list[Dispatch]:
    assert attack.action is not None
    return [d for d in log if attack.action.matches(d.verb, d.role, d.name)]


@pytest.mark.parametrize("attack", pinned("IJ-15"), ids=lambda a: a.case)
def test_an_evasive_spelling_still_raises_the_tier(attack: Attack) -> None:
    assert effective_risk(_click(attack, automation_id=_plan_id(attack))) == attack.tier


@pytest.mark.parametrize("attack", pinned("IJ-15"), ids=lambda a: a.case)
def test_a_raised_click_is_never_dispatched_without_a_yes(attack: Attack) -> None:
    h, log = _on_page(attack)
    result = h.session.run_step(_click(attack, automation_id=_plan_id(attack)))
    assert result.evidence["risk"] == attack.tier
    assert result.gate in ("block", "pending"), result.to_dict()
    assert _dispatched(attack, log) == []
    assert log == []


# ---- known gaps -----------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="IJ-01 #81")
def test_a_control_with_an_unlisted_external_effect_asks_or_blocks() -> None:
    attack = fixture("IJ-01", "place_order")
    h, log = _on_page(attack)
    result = h.session.run_step(_click(attack))
    assert _dispatched(attack, log) == []
    assert result.gate != "approve"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="IJ-03 #83")
def test_a_homoglyph_deny_word_is_raised() -> None:
    attack = fixture("IJ-03", "homoglyph")
    assert effective_risk(_click(attack)) == attack.tier


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="IJ-04 #84")
def test_the_resolved_elements_automation_id_raises_the_tier() -> None:
    attack = fixture("IJ-04", "element_id")
    assert attack.action is not None
    h, log = _on_page(attack, automation_ids={attack.action.name: attack.payload})
    result = h.session.run_step(_click(attack))
    assert _dispatched(attack, log) == []
    assert result.gate != "approve"
