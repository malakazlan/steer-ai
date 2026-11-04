"""A target missing on a fresh observe right after the page changed is a timing gap, not an
absence (first real-site run, 2026-11-04; RN-18 generalised): the loop settles and re-observes a
bounded number of times before `no_match`."""

from __future__ import annotations

from typing import Any

from steerai.errors import ErrorCode
from steerai.orchestrator.loop import SETTLE_RETRIES, SETTLE_S
from steerai.verify.predicates import TitleMatches
from steerai.world.model import Snapshot
from tests.orchestrator.fakes import FakeWorld, Row
from tests.orchestrator.test_loop import Harness, _click

LATE_BUTTON: Row = (30, "Button", "Google Search", (42, 70), 7)


class _Appearing:
    """The target is absent for the first `missing` observes, then present."""

    def __init__(self, world: FakeWorld, *, missing: int) -> None:
        self.world = world
        self.missing = missing
        self.calls = 0

    def observe(self, hwnd: int, **kwargs: Any) -> Snapshot:
        self.calls += 1
        if self.calls == self.missing + 1 and LATE_BUTTON not in self.world.rows:
            self.world.rows.append(LATE_BUTTON)
        return self.world.observe(hwnd, **kwargs)


def _rebuild(h: Harness, observer: Any) -> None:
    from steerai.orchestrator.gate import Gate  # noqa: PLC0415
    from steerai.orchestrator.loop import Session  # noqa: PLC0415
    from tests.orchestrator.fakes import FakeLive  # noqa: PLC0415

    h.session = Session(
        task_id="task-1",
        hwnd=0x10,
        observer=observer,
        semantic=h.semantic,
        physical=h.physical,
        gate=Gate(confirmer=h.confirmer, own_pid=4242, task_id="task-1"),
        ledger=h.ledger,
        live=FakeLive(h.world),
        trace=h.trace,
        monotonic=lambda: h.clock["t"],
        sleep=lambda s: h.clock.__setitem__("t", h.clock["t"] + s),
        verify_deadline_s=0.5,
    )


def test_a_target_that_appears_after_one_settle_is_found() -> None:
    h = Harness()
    observer = _Appearing(h.world, missing=1)
    _rebuild(h, observer)
    started = h.clock["t"]
    result = h.session.run_step(
        _click(step_id="g", name="Google Search", expect=TitleMatches(pattern="Nothing - Chat"))
    )
    assert result.action is not None, result.to_dict()
    assert result.evidence["settled"] == 1
    assert h.clock["t"] - started >= SETTLE_S
    assert h.semantic.calls[0][1] == 30


def test_a_target_still_missing_after_the_retries_is_no_match() -> None:
    h = Harness()
    observer = _Appearing(h.world, missing=SETTLE_RETRIES + 5)
    _rebuild(h, observer)
    result = h.session.run_step(
        _click(step_id="g", name="Google Search", expect=TitleMatches(pattern="Nothing - Chat"))
    )
    assert result.code is ErrorCode.ELEMENT_NOT_FOUND
    assert result.evidence["detail"] == "no_match"
    assert result.evidence["settled"] == SETTLE_RETRIES
    assert observer.calls == SETTLE_RETRIES + 1, "one observe per retry, then fail"
    assert h.semantic.calls == []


def test_an_ambiguous_target_does_not_settle() -> None:
    h = Harness()
    h.world.rows.append((31, "Button", "Chat options", (42, 71), 7))  # a twin of the base button
    observer = _Appearing(h.world, missing=99)
    _rebuild(h, observer)
    result = h.session.run_step(_click(expect=TitleMatches(pattern="Nothing - Chat")))
    assert result.evidence["detail"] == "ambiguous_target"
    assert "settled" not in result.evidence
    assert observer.calls == 1
