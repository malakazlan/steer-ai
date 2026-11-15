"""[V2.57] LW-02: v2 tags its input with a random per-run value; v1 keeps 0x53544545. Every
record builder reads the one process-wide tag."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from steerai.probes.win_input import (
    STEER_EXTRA_INFO,
    build_keyboard_input,
    build_keyboard_unicode,
    build_mouse_input,
    extra_info,
    use_extra_info,
)


@pytest.fixture(autouse=True)
def _restore() -> Iterator[None]:
    before = extra_info()
    yield
    use_extra_info(before)


def test_v1s_tag_is_the_default() -> None:
    assert extra_info() == STEER_EXTRA_INFO
    assert build_mouse_input(dx=0, dy=0, flags=0).union.mi.dwExtraInfo == STEER_EXTRA_INFO


def test_every_record_carries_the_runs_tag() -> None:
    use_extra_info(0x1234_5678)
    assert build_mouse_input(dx=0, dy=0, flags=0).union.mi.dwExtraInfo == 0x1234_5678
    assert build_keyboard_input(vk=0x41, keyup=False).union.ki.dwExtraInfo == 0x1234_5678
    assert build_keyboard_unicode(0x41, keyup=False).union.ki.dwExtraInfo == 0x1234_5678


@pytest.mark.parametrize("tag", [0, -1, 1 << 32])
def test_a_tag_is_a_nonzero_32_bit_value(tag: int) -> None:
    with pytest.raises(ValueError, match="tag"):
        use_extra_info(tag)
    assert extra_info() == STEER_EXTRA_INFO


def test_a_bound_tag_wins_over_the_process_tag() -> None:
    """#385 HH4-11: a probe bound to the run's tag keeps it after the process tag is restored."""
    use_extra_info(STEER_EXTRA_INFO)
    run = 0x2222_3333
    assert build_mouse_input(dx=0, dy=0, flags=0, tag=run).union.mi.dwExtraInfo == run
    assert build_keyboard_input(vk=0x0E, keyup=True, tag=run).union.ki.dwExtraInfo == run
