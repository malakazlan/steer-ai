"""IJ-21: the page changes between the person's yes and the act (OR-31, RN-13; #89). A window
title the page relabels during the ask, a target relabelled during the ask, and two labels
swapped during the ask all fail the step `screen_changed_during_ask` with nothing sent: after
the yes an exact target must still be the element asked about (RuntimeId and role). The same
binding holds inside the act loop: a page that swaps labels on the first dispatch (the executor
refuses `identity_changed`) or before the scroll's re-resolve fails `screen_changed_during_act`
and the retry never reaches another element. Person rows are still rebound by their full name
after a yes (IJ-21p, a strict xfail)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from steerai.errors import ErrorCode
from steerai.orchestrator.contracts import StepResult
from steerai.orchestrator.gate import Ask
from tests.orchestrator.fakes import CHAT_AMIR, FakeExecutor, not_delivered
from tests.orchestrator.fakes import SEND as SEND_ROW
from tests.orchestrator.test_loop import Harness, _send, _type
from tests.orchestrator.test_person_loop_edges import _r2_click
from tests.stress.injection.catalogue import fixture
from tests.stress.injection.hostile import Dispatch, HostilePage, rebuild


def _rename(h: Harness, names: dict[str, str]) -> None:
    """The page's script: rename rows by their current name."""
    h.world.rows = [(r[0], r[1], names.get(r[2], r[2]), r[3], r[4]) for r in h.world.rows]


def _relabel(h: Harness, names: dict[str, str]) -> Callable[[Ask], None]:
    """The page's script, run while the ask is open."""

    def during_ask(_ask: Ask) -> None:
        _rename(h, names)

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
    attack = fixture("IJ-21", "target_relabel")
    h = Harness()
    log: list[Dispatch] = []
    rebuild(h, log=log)
    h.confirmer.on_ask = _relabel(h, {"Send": attack.payload})
    _ask, sent, result = _run(h, log)
    assert sent == []
    assert h.world.sent == []
    assert result.ok is False


def test_a_target_relabelled_during_the_ask_is_screen_changed_during_ask() -> None:
    attack = fixture("IJ-21", "target_relabel")
    h = Harness()
    log: list[Dispatch] = []
    rebuild(h, log=log)
    h.confirmer.on_ask = _relabel(h, {"Send": attack.payload})
    h.session.run_step(_type())
    result = h.session.run_step(_send())
    assert (result.code, result.evidence.get("detail")) == (ErrorCode.FOCUS_LOST, attack.detail)


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


# ---- inside the act loop: the swap lands after the yes, on the first dispatch -----------------


def _swap_on_first_click(h: Harness, payload: str, refusal: str) -> None:
    """The page swaps `Send` and `Delete chat` as the first click arrives; the executor refuses
    that click (`identity_changed`: the live name no longer matches; `offscreen`: it asks for
    the scroll), so the act loop re-resolves on a page that moved the label."""
    clicks = [0]

    def on_click(executor: FakeExecutor, action: Any) -> None:
        if action.verb != "click":
            return
        clicks[0] += 1
        if clicks[0] == 1:
            _rename(h, {"Send": payload, "Delete chat": "Send"})
            code = (
                ErrorCode.ELEMENT_NOT_FOUND
                if refusal == "identity_changed"
                else ErrorCode.ELEMENT_NOT_ACTIONABLE
            )
            executor.outcomes[action.element_id] = not_delivered(code, refusal)

    h.semantic.on_execute = on_click


@pytest.mark.parametrize("refusal", ["identity_changed", "offscreen"])
def test_labels_swapped_on_the_first_dispatch_never_move_the_retry(refusal: str) -> None:
    attack = fixture("IJ-21", "act_swap")
    h = Harness()
    log: list[Dispatch] = []
    rebuild(h, log=log)
    h.session.run_step(_type())
    log.clear()
    _swap_on_first_click(h, attack.payload, refusal)
    result = h.session.run_step(_send())
    assert result.gate == "confirm", "the person did say yes"
    assert [d.runtime_id for d in log if d.runtime_id != SEND_ROW[3]] == [], "another element"
    assert (result.code, result.evidence.get("detail")) == (attack.code, attack.detail)
    assert h.world.sent == []


# ---- person rows: the full-name fallback after a yes (IJ-21p) ------------------------------------


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="IJ-21p #92")
def test_person_rows_swapping_full_names_after_the_yes_never_move_the_click() -> None:
    attack = fixture("IJ-21", "person_swap")
    h = Harness()
    log: list[Dispatch] = []
    rebuild(h, log=log)
    h.world.rows.append((15, "ListItem", attack.payload, (42, 17), 1))

    def reply(ask: Ask) -> None:
        if ask.kind == "which":
            h.confirmer.answers.append(f"1:{ask.decision_id}")  # the person picks their row
        else:
            _rename(h, {CHAT_AMIR[2]: attack.payload, attack.payload: CHAT_AMIR[2]})

    h.confirmer.on_ask = reply
    result = h.session.run_step(_r2_click())
    assert [d.runtime_id for d in log if d.runtime_id != CHAT_AMIR[3]] == [], "another row"
    assert result.ok is False
