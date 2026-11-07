"""Deny words (5.13 1a; IJ-01, IJ-03, IJ-04 and IJ-15 pinned): a page
that names a control to slip past the deny matcher still has the step raised, so the gate asks
or blocks and nothing is dispatched without the person's yes."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from steerai.goal_match import Judgement
from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.gate import effective_risk
from steerai.verify.predicates import TitleMatches
from tests.orchestrator.test_loop import Harness, _plan
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


def test_a_control_with_an_unlisted_external_effect_asks_or_blocks() -> None:
    """No deny word names it; goal-match (5.13 step 2 [v2.27]) judges it unrelated to the
    request and the step asks. The matcher here is scripted: the stand-in's own catch rate is
    the evaluation probe's (`steerai.probes.goal_match_eval`)."""
    attack = fixture("IJ-01", "place_order")
    h, log = _on_page(attack)
    h.session._goal_matcher = _Unrelated()
    result = h.session.run_task(_plan(_click(attack)))
    assert _dispatched(attack, log) == []
    assert result.results[0].gate != "approve"
    assert [ask.reasons for ask in h.confirmer.asked] == [("goal_unrelated",)]


class _Unrelated:
    def judge(
        self,
        goal: str,  # noqa: ARG002 - the protocol's shape
        steps: Sequence[Step],
        done: Sequence[Step] = (),  # noqa: ARG002 - the protocol's shape
    ) -> Judgement:
        return Judgement({s.id: "unrelated" for s in steps}, model="scripted", latency_ms=1.0)


def test_a_homoglyph_deny_word_is_raised() -> None:
    attack = fixture("IJ-03", "homoglyph")
    assert effective_risk(_click(attack)) == attack.tier


def test_the_resolved_elements_automation_id_raises_the_tier() -> None:
    """The plan names "OK" with no id; the page's element carries btnDeleteAccount. The tier
    reads the resolved element's id at admission, the settled tier and the gate (#84)."""
    attack = fixture("IJ-04", "element_id")
    assert attack.action is not None
    h, log = _on_page(attack, automation_ids={attack.action.name: attack.payload})
    result = h.session.run_step(_click(attack))
    assert _dispatched(attack, log) == []
    assert result.evidence["risk"] == "R3"
    assert result.gate == "block", "a raised click without a strong expect blocks ([v2.17] b)"
    assert result.evidence["detail"] == "weak_predicate"


def test_admission_runs_at_the_tier_the_page_element_raises_to() -> None:
    """The ceiling reads the ids of every element the exact target could resolve to, so an
    R3 key is admitted (done, open records) before any resolve or dispatch (#84)."""
    attack = fixture("IJ-04", "element_id")
    assert attack.action is not None
    h, _ = _on_page(attack, automation_ids={attack.action.name: attack.payload})
    step = _click(attack)
    before = h.session._observer.observe(0x10)
    assert h.session._ceiling(step, h.ledger.key_for(step), before, "R0") == "R3"


class _LateRaised:
    """The plan's "OK" is absent on the first look and appears after the settle carrying the
    page's btnDeleteAccount: the ceiling saw nothing, the settled tier rises (#84 review)."""

    def __init__(self, page: HostilePage, row: tuple[int, str, str, tuple[int, ...], int]) -> None:
        self.page = page
        self.row = row
        self.calls = 0

    def observe(self, hwnd: int, **kwargs: object) -> object:
        self.calls += 1
        if self.calls == 2 and self.row not in self.page.world.rows:
            self.page.world.rows.append(self.row)
        return self.page.observe(hwnd, **kwargs)


def _late(attack: Attack) -> tuple[Harness, list[Dispatch]]:
    assert attack.action is not None
    h = Harness(default="no")
    log: list[Dispatch] = []
    page = HostilePage(h.world, {attack.action.name: attack.payload})
    row = (11, "Button", attack.action.name, (42, 43), 7)
    rebuild(h, log=log, observer=_LateRaised(page, row))
    return h, log


def test_a_done_key_found_only_after_the_settle_stops_and_dispatches_nothing() -> None:
    attack = fixture("IJ-04", "element_id")
    h, log = _late(attack)
    step = _click(attack)
    h.ledger.executions[h.ledger.key_for(step)] = {"step_id": "c0", "outcome": "delivered_verified"}
    result = h.session.run_step(step)
    assert result.evidence["detail"] == "done_at_raised_tier", result.to_dict()
    assert result.code is not None
    assert log == []


def test_an_open_record_found_only_after_the_settle_reaches_the_gate() -> None:
    attack = fixture("IJ-04", "element_id")
    h, log = _late(attack)
    step = _click(attack)
    h.ledger.executions[h.ledger.key_for(step)] = {
        "step_id": "c0",
        "outcome": "delivered_unverified",
    }
    result = h.session.run_step(step)
    assert result.evidence["risk"] == "R3"
    assert log == []
    assert result.gate == "block", "the raised click still has no strong expect"


def test_a_done_key_with_the_element_visible_is_done_already_at_the_ceiling() -> None:
    attack = fixture("IJ-04", "element_id")
    assert attack.action is not None
    h, log = _on_page(attack, automation_ids={attack.action.name: attack.payload})
    step = _click(attack)
    h.ledger.executions[h.ledger.key_for(step)] = {"step_id": "c0", "outcome": "delivered_verified"}
    result = h.session.run_step(step)
    assert result.gate == "done_already"
    assert log == []
