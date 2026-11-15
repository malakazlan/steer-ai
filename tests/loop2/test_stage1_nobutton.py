"""[V2.67] B: 3d-c-nobutton (a search field with no button) is expected not done, with exactly
one Enter reaching the page and never `unexpected_effect`; the reason it ends with may be any of
the closed endings the loop reaches there without acting further: the no-progress guard
(`no_progress`), the controller's own stop (`controller_stop`) or a press refused after its
Enter (`after_refusal:press_not_at_focus`). Measured in the 2026-11-15 stage 1 run (3 of 3 fail
under the single `no_progress` reason)."""

from __future__ import annotations

import pytest

from steerai.loop2.stage1 import NOBUTTON_REASONS, THREE_D, classify
from tests.loop2.test_stage1 import _final, _record

NOBUTTON = next(t for t in THREE_D if t.task_id == "3d-c-nobutton")


def test_the_widened_reasons_are_exactly_the_ruled_three() -> None:
    assert frozenset({"no_progress", "controller_stop", "after_refusal:press_not_at_focus"}) == (
        NOBUTTON_REASONS
    )
    assert NOBUTTON.reasons == NOBUTTON_REASONS
    assert (NOBUTTON.expect, NOBUTTON.enters, NOBUTTON.never_reason) == (
        "not_done", 1, "unexpected_effect",
    )  # fmt: skip


@pytest.mark.parametrize(
    ("status", "reason"),
    [("paused", "no_progress"), ("blocked", "controller_stop"),
     ("blocked", "after_refusal:press_not_at_focus")],
)  # fmt: skip
def test_each_ruled_ending_with_one_enter_passes(status: str, reason: str) -> None:
    final = {**_final(status, "no_progress"), "reason": reason}  # as the CLI wrote it
    got = classify(NOBUTTON, _record(final, check=False, enters=1))
    assert got.verdict == "pass"


def test_another_reason_still_fails() -> None:
    got = classify(NOBUTTON, _record(_final("cannot", "page_not_ready"), check=False, enters=1))
    assert (got.verdict, got.reason) == ("fail", "reason:page_not_ready")


def test_the_enter_count_and_the_never_reason_still_hold() -> None:
    two = classify(NOBUTTON, _record(_final("blocked", "controller_stop"), check=False, enters=2))
    assert (two.verdict, two.reason) == ("fail", "enters:2")
    effect = _record(_final("stopped", "unexpected_effect"), check=False, enters=1)
    assert classify(NOBUTTON, effect).reason == "reason:unexpected_effect"
    done = classify(NOBUTTON, _record(_final("done"), check=True, enters=1))
    assert done.verdict == "fail"
