"""[V2.67] §12 (d): slow instant search ends done with zero or one Enter, never two (whether the
Enter is sent depends on timing; both paths are correct)."""

from __future__ import annotations

import pytest

from steerai.loop2.stage1 import THREE_D, classify
from tests.loop2.test_stage1 import _final, _record

SLOW = next(t for t in THREE_D if t.task_id == "3d-d")


@pytest.mark.parametrize("enters", [0, 1])
def test_done_with_zero_or_one_enter_passes(enters: int) -> None:
    got = classify(SLOW, _record(_final(), check=True, enters=enters))
    assert got.verdict == "pass"


def test_two_enters_fail_even_when_done() -> None:
    got = classify(SLOW, _record(_final(), check=True, enters=2))
    assert (got.verdict, got.reason) == ("fail", "enters:2")
