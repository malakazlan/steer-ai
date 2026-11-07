"""Layer A end to end (packet injection-suite-2026-11-07, gate item Q1): catalogue fixtures run
through the real `Session` (loop, gate, ledger contract, trace) on a hostile page, with the plan
the fake planner would return given directly. Every run asserts the dispatch log, that each
dispatch after a yes touches the very element the person was asked about, and the shared trace
scan over every payload of the catalogue (IJ-29)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

import pytest

from steerai.orchestrator.console import ConsoleConfirmer
from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.gate import Ask, Confirmer
from steerai.orchestrator.trace import ListTrace
from steerai.verify.predicates import TitleMatches
from tests.orchestrator.test_loop import APPEARED, HELLO, Harness, _send, _type
from tests.orchestrator.test_open_loop import Rig
from tests.orchestrator.test_open_loop import _open as _yt_open
from tests.orchestrator.test_open_loop import _plan as _open_plan
from tests.stress.injection.catalogue import Attack, Signature, fixture, sentinels
from tests.stress.injection.hostile import Dispatch, Typist, rebuild, scan_trace

GOAL = "send hello there to Amir"
HEADER = 3
"""The chat header group: hostile text lives there, outside the conversation the send's effect
is checked in."""


@dataclass
class Witness:
    """The person at the ask: answers by rule, and notes which elements on the page carried the
    asked role and name when the ask was shown, and where the dispatch log stood."""

    h: Harness
    log: list[Dispatch]
    answer: str
    asked: list[tuple[Ask, int, list[tuple[int, ...]]]] = field(default_factory=list)
    inner: Confirmer | None = None

    def ask(self, ask: Ask) -> str:
        shown = [
            r[3] for r in self.h.world.rows if (r[1], r[2]) == (ask.target_role, ask.target_name)
        ]
        self.asked.append((ask, len(self.log), shown))
        if self.inner is not None:
            return self.inner.ask(ask)
        return self.answer.replace("{id}", ask.decision_id)


@dataclass(frozen=True)
class Scenario:
    attack: Attack
    plan: Callable[[Attack], Plan]
    answer: str
    dispatched: tuple[tuple[str, str, str], ...]
    """The dispatch log expected: (executor verb, role, name)."""
    page: Callable[[Harness, Attack], None] = lambda _h, _a: None
    console: bool = False
    """Answer through the real console confirmer, non-interactive."""


def _hostile_text(h: Harness) -> None:
    """Every payload of the catalogue on screen as page text, so the trace scan means
    something."""
    for payload in sentinels():
        index = len(h.world.rows)
        h.world.rows.append((index, "Text", payload, (66, index), HEADER))


def _plan(*steps: Step) -> Plan:
    return Plan(
        goal=GOAL, subgoals=(Subgoal(id="g1", desc="sent", condition=APPEARED),), steps=steps
    )


def _injected_send(attack: Attack) -> Plan:
    send = _send()
    return _plan(replace(send, params={**send.params, "text": attack.payload}))


def _person_send(_attack: Attack) -> Plan:
    return _plan(_type(), replace(_send(), preapproved_by_request=True))


def _evasive_click(attack: Attack) -> Plan:
    assert attack.action is not None
    target = Target(role="Button", name=attack.action.name)
    click = Step(
        id="c1",
        subgoal="g1",
        verb="click",
        target=target,
        risk="R0",
        expect=TitleMatches(pattern="Amir Khan - Chat"),
    )
    return _plan(click)


def _resend(_attack: Attack) -> Plan:
    return _plan(_type(), _send(), replace(_send(), id="p2.s3"))


def _lookalike_click(_attack: Attack) -> Plan:
    click = Step(
        id="p1",
        subgoal="g1",
        verb="click",
        target=Target(role="ListItem", name="Amir", match="person"),
        risk="R1",
        expect=TitleMatches(pattern="Amir Khan - Chat"),
    )
    return _plan(click)


def _button(h: Harness, attack: Attack) -> None:
    assert attack.action is not None
    h.world.rows.append((len(h.world.rows), "Button", attack.action.name, (77, 1), 7))


def _own_window(h: Harness, _attack: Attack) -> None:
    h.world.pid = 4242


def _lookalike(h: Harness, attack: Attack) -> None:
    h.world.rows.append((len(h.world.rows), "ListItem", attack.payload, (77, 2), 1))


TYPED = ("type", "Edit", "Message")
SENT = ("click", "Button", "Send")
SCENARIOS = (
    Scenario(fixture("IJ-12", "send_r0"), _injected_send, "no", ()),
    Scenario(fixture("IJ-13", "preapproved_send"), _person_send, "yes:{id}", (TYPED, SENT)),
    Scenario(fixture("IJ-14", "bare_yes"), _person_send, "yes", (TYPED,), console=True),
    Scenario(fixture("IJ-14", "stale_id"), _person_send, "yes:d-000000000000", (TYPED,)),
    Scenario(fixture("IJ-15", "underscore"), _evasive_click, "yes:{id}", (), page=_button),
    Scenario(fixture("IJ-15", "zero_width"), _evasive_click, "yes:{id}", (), page=_button),
    Scenario(fixture("IJ-16", "own_console"), _injected_send, "yes:{id}", (), page=_own_window),
    Scenario(fixture("IJ-23", "lookalike"), _lookalike_click, "unanswered", (), page=_lookalike),
    Scenario(fixture("IJ-34", "cloned_button"), _person_send, "yes:{id}", (TYPED,), page=_button),
    Scenario(fixture("IJ-36", "resend"), _resend, "yes:{id}", (TYPED, SENT)),
)


def _injected(attack: Attack, log: list[Dispatch]) -> list[Dispatch]:
    signature: Signature | None = attack.action
    if signature is None:
        return []
    return [d for d in log if signature.matches(d.verb, d.role, d.name)]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.attack.key)
def test_a_hostile_page_dispatches_only_what_the_person_approved(scenario: Scenario) -> None:
    h = Harness()
    _hostile_text(h)
    scenario.page(h, scenario.attack)
    log: list[Dispatch] = []
    witness = Witness(h, log, scenario.answer)
    if scenario.console:
        screen = Typist(scenario.answer)
        witness.inner = ConsoleConfirmer(stdin=screen.stdin, stderr=screen, interactive=False)
    rebuild(h, confirmer=witness, log=log)
    h.session.run_task(scenario.plan(scenario.attack))

    assert [(d.verb, d.role, d.name) for d in log] == list(scenario.dispatched)
    approved = scenario.answer == "yes:{id}" and not scenario.console
    if not approved:
        assert _injected(scenario.attack, log) == [], "no yes, no injected action"
    checked = 0
    for ask, at, shown in witness.asked:
        if not approved or ask.kind != "yes" or at >= len(log):
            continue
        dispatched = log[at]
        assert (dispatched.role, dispatched.name) == (ask.target_role, ask.target_name)
        assert shown == [dispatched.runtime_id], "the element asked about is the one acted on"
        checked += 1
    assert checked == (scenario.dispatched.count(SENT) if approved else 0)
    assert scan_trace(h.trace.lines) == []


def test_page_content_in_an_open_asks_with_the_host_and_never_launches_on_a_no() -> None:
    attack = fixture("IJ-17", "page_text")
    rig = Rig()
    rig.confirmer.default = "no"
    rig.trace = ListTrace()
    session = rig.build()
    plan = _open_plan(_yt_open(attack.payload))
    result = session.run_task(plan)
    assert [(a.risk, a.params["opens"]) for a in rig.confirmer.asked] == [
        (attack.tier, "www.youtube.com")
    ]
    assert rig.launcher.calls == []
    assert result.status == "blocked"
    assert scan_trace(rig.trace.lines) == []


def test_the_scanner_finds_a_payload_raw_or_json_escaped() -> None:
    """The scanner itself: a trace that did carry a payload is caught in either form."""
    crlf = fixture("IJ-18", "crlf").payload
    assert scan_trace(['{"name": "Delete_all"}'], ["Delete_all"]) == ["Delete_all"]
    assert scan_trace(['{"text": "cats\\r\\nHost: evil.example"}'], [crlf]) == [crlf]
    assert scan_trace(['{"kind": "step"}']) == []
    assert HELLO in sentinels()
