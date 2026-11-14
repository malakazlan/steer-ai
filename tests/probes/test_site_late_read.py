"""[V2.27] B: the site-sample probe takes one more full read about `LATE_S` after `watch` returns
and records its classes against the before, with a flag when they differ from the watch's final
report (a page that navigated after an early, small change). Fakes only: no browser."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from steerai.errors import ErrorCode, SteerError
from steerai.probes.effect_pages import LATE_S, site_sample
from steerai.world.effects import Observed
from tests.world.test_effects_conditions import BASE, Row, _snap

URL = "https://shop.test/"
RESULTS_URL = "https://shop.test/results?q=red"
NEW_PAGE: Row = (1, "Document", "Results", (9, 77), None)
NEW_ITEM: Row = (5, "ListItem", "Red shoes", (9, 5), 3)


def _obs(rows: list[Row], url: str = URL) -> Observed:
    return Observed(snapshot=_snap(rows), url=url, windows=(16,))


BEFORE = _obs(BASE)
CONTENT = _obs([*BASE, NEW_ITEM])
"""A small early change on the start page: content changed, URL and Document unchanged."""
NAVIGATED = _obs([NEW_PAGE, (3, "List", "Products", (9, 78), 1)], RESULTS_URL)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class _Bar:
    def address_bar(self, hwnd: int) -> str | None:  # noqa: ARG002 - the reader's shape
        return "shop.test/results?q=red"


class FakeBrowser:
    """`read_v2` gives the before, then the late read; every poll and full read gives `seen`."""

    hwnd = 0x10

    def __init__(self, clock: Clock, seen: Observed, late: Callable[[], Observed]) -> None:
        self._clock, self._seen, self._late = clock, seen, late
        self.v2_reads: list[float] = []
        self.scripts: list[str] = []

    @property
    def client(self) -> _Bar:
        return _Bar()

    def run(self, script: str) -> Any:  # Any: the page script's JSON value
        self.scripts.append(script)
        return RESULTS_URL

    def read_cheap(self) -> Observed:
        return self._seen

    def read_full(self, seen: Observed) -> Observed:
        return seen

    def read_v2(self) -> Observed:
        self.v2_reads.append(self._clock.t)
        return BEFORE if len(self.v2_reads) == 1 else self._late()


def _sample(seen: Observed, late: Callable[[], Observed]) -> tuple[dict[str, Any], FakeBrowser]:
    clock = Clock()
    browser = FakeBrowser(clock, seen, late)
    row = site_sample(browser, RESULTS_URL, sleep=clock.sleep, clock=clock.now)
    return row, browser


def test_a_page_that_navigates_after_watch_returned_is_flagged_late_changed() -> None:
    row, _ = _sample(CONTENT, lambda: NAVIGATED)
    assert row["classes"] == ["content_changed"]
    assert row["late_classes"] == ["navigated"]
    assert row["late_changed"] is True


def test_a_late_read_that_agrees_with_the_watch_is_not_flagged() -> None:
    row, _ = _sample(NAVIGATED, lambda: NAVIGATED)
    assert row["classes"] == ["navigated"]
    assert row["late_classes"] == ["navigated"]
    assert row["late_changed"] is False


def test_the_late_read_is_one_full_read_late_s_after_watch_returned() -> None:
    _, browser = _sample(NAVIGATED, lambda: NAVIGATED)
    assert len(browser.v2_reads) == 2, "the before and exactly one late read"
    before_at, late_at = browser.v2_reads
    assert late_at - before_at >= LATE_S
    assert LATE_S == 5.0


def test_a_failed_late_read_keeps_the_row_and_says_so() -> None:
    def gone() -> Observed:
        raise SteerError(ErrorCode.APP_CRASHED, "window gone")

    row, _ = _sample(NAVIGATED, gone)
    assert row["classes"] == ["navigated"]
    assert row["late_classes"] is None
    assert row["late_changed"] is None
    assert row["late_error"] == "SteerError"


def test_the_row_keeps_its_closed_tokens_and_no_url() -> None:
    row, _ = _sample(CONTENT, lambda: NAVIGATED)
    assert row["bar_shape"] == "no_scheme"
    assert "shop.test" not in str(row)
