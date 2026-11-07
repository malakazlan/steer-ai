"""IJ-21: the page changes between the person's yes and the act (OR-31, RN-13). A window title
the page relabels during the ask fails the step `screen_changed_during_ask` with nothing sent
(pinned). Two variants do not hold as the catalogue states them (finding #89): a relabelled
target fails `no_match` rather than `screen_changed_during_ask` (nothing is sent), and two
labels swapped during the ask send the click to another element than the one asked about,
because the re-resolve after the yes matches by role and name, not by the element asked on."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import StepResult
from steerai.orchestrator.gate import Ask
from tests.orchestrator.fakes import SEND as SEND_ROW
from tests.orchestrator.test_loop import Harness, _send, _type
from tests.stress.injection.catalogue import fixture
from tests.stress.injection.hostile import Dispatch, HostilePage, rebuild


def _relabel(h: Harness, names: dict[str, str]) -> Callable[[Ask], None]:
    """The page's script, run while the ask is open: rename rows by their current name."""

    def during_ask(_ask: Ask) -> None:
        h.world.rows = [(r[0], r[1], names.get(r[2], r[2]), r[3], r[4]) for r in h.world.rows]

    return during_ask


def _run(h: Harness, log: list[Dispatch]) -> tuple[Ask, list[Dispatch], StepResult]:
    h.session.run_step(_type())
    log.clear()
    result = h.session.run_step(_send())
    return h.confirmer.asked[0], list(log), result


def test_a_title_the_page_relabels_during_the_ask_stops_the_step() -> None:
    attack = fixture("IJ-21", "title_relabel")
    h = Harness()
    page = HostilePage(h.world)
    log: list[Dispatch] = []
    rebuild(h, observer=page, log=log)
    h.session.run_step(_type())
    log.clear()

    def retitle(_ask: Ask) -> None:
        page.title = attack.payload

    h.confirmer.on_ask = retitle
    result = h.session.run_step(_send())
    assert result.gate == "confirm", "the person did say yes"
    assert (result.code, result.evidence.get("detail")) == (attack.code, attack.detail)
    assert log == []
    assert h.world.sent == []


def test_a_target_relabelled_during_the_ask_is_never_dispatched() -> None:
    """What holds of the relabel variant today: nothing is sent."""
    attack = fixture("IJ-21", "target_relabel")
    h = Harness()
    log: list[Dispatch] = []
    rebuild(h, log=log)
    h.confirmer.on_ask = _relabel(h, {"Send": attack.payload})
    _ask, sent, result = _run(h, log)
    assert sent == []
    assert h.world.sent == []
    assert result.ok is False


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="IJ-21 #89")
def test_a_target_relabelled_during_the_ask_is_screen_changed_during_ask() -> None:
    attack = fixture("IJ-21", "target_relabel")
    h = Harness()
    log: list[Dispatch] = []
    rebuild(h, log=log)
    h.confirmer.on_ask = _relabel(h, {"Send": attack.payload})
    h.session.run_step(_type())
    result = h.session.run_step(_send())
    assert (result.code, result.evidence.get("detail")) == (ErrorCode.FOCUS_LOST, attack.detail)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="IJ-21 #89")
def test_labels_swapped_during_the_ask_never_move_the_click_to_another_element() -> None:
    attack = fixture("IJ-21", "label_swap")
    h = Harness()
    log: list[Dispatch] = []
    rebuild(h, log=log)
    h.confirmer.on_ask = _relabel(h, {"Send": attack.payload, "Delete chat": "Send"})
    h.session.run_step(_type())
    log.clear()
    result = h.session.run_step(_send())
    assert [d.runtime_id for d in log if d.runtime_id != SEND_ROW[3]] == [], "another element"
    assert (result.code, result.evidence.get("detail")) == (attack.code, attack.detail)
