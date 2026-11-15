"""#377: which of `PhysicalExecutor._click`'s `target_moved` paths fired, named from the calls the
click made after its cursor move. Test-only: v1's executor is untouched; the live tree test
records its inputs and prints this on failure."""

from __future__ import annotations

from typing import Any

from tests.orchestrator.click_evidence import moved_evidence

HWND = 0x1234
LIVE = (10, 20, 110, 40)
WINDOW = (0, 0, 1100, 900)
SCREEN = (0, 0, 1920, 1080)


def _click(*after: tuple[str, tuple[Any, ...], Any]) -> list[tuple[str, tuple[Any, ...], Any]]:
    """An earlier admission's reads, then the click's own move and what followed it."""
    return [
        ("root_hwnd_at", (60, 30), HWND),
        ("runtime_id_at", (60, 30), (42, 1)),
        ("move_cursor", (60, 30), None),
        ("cursor_pos", (), (60, 30)),
        ("live_rect", (), LIVE),
        ("window_rect", (HWND,), WINDOW),
        ("screen_rect", (), SCREEN),
        *after,
    ]


def test_no_hit_test_after_the_last_look_means_the_aim_point_moved() -> None:
    evidence = moved_evidence(_click(), HWND)
    assert evidence == {
        "path": "aim_changed",
        "clicked_at": [60, 30],
        "cursor_at": [60, 30],
        "live_rect": list(LIVE),
        "window_rect": list(WINDOW),
        "screen_rect": list(SCREEN),
    }


def test_another_root_window_under_the_point_is_the_window_path() -> None:
    evidence = moved_evidence(_click(("root_hwnd_at", (60, 30), 0x9999)), HWND)
    assert (evidence["path"], evidence["root_at_point"]) == ("root_window", 0x9999)


def test_the_right_window_but_another_element_is_the_uia_path() -> None:
    calls = _click(("root_hwnd_at", (60, 30), HWND), ("runtime_id_at", (60, 30), (42, 7)))
    evidence = moved_evidence(calls, HWND)
    assert (evidence["path"], evidence["hit"]) == ("uia_element", [42, 7])


def test_a_refusal_before_any_cursor_move_says_so() -> None:
    assert moved_evidence([("root_hwnd_at", (60, 30), HWND)], HWND) == {"path": "no_click"}


def test_only_the_last_clicks_calls_count() -> None:
    first = _click(("root_hwnd_at", (60, 30), HWND), ("runtime_id_at", (60, 30), (42, 1)))
    evidence = moved_evidence([*first, *_click()], HWND)
    assert evidence["path"] == "aim_changed"
