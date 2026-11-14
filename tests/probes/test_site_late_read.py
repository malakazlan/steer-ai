"""[V2.27] B: the site-sample probe takes one more full read about `LATE_S` after `watch` returns
and records its classes against the before, with a flag when they differ from the watch's final
report (a page that navigated after an early, small change). [V2.28] 1: the watch is a submit,
held to the guard unless a conclusive report arrives, and the row says when one did. Fakes
only: no browser."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from steerai.errors import ErrorCode, SteerError
from steerai.probes.effect_pages import LATE_S, SITE_READY_S, site_sample
from steerai.world.effects import Observed
from steerai.world.watch import SUBMIT_GUARD_S
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


class LateNavigation(FakeBrowser):
    """A small change at once, then the navigation `after_s` into the watch."""

    def __init__(self, clock: Clock, after_s: float) -> None:
        super().__init__(clock, CONTENT, lambda: NAVIGATED)
        self._at = after_s  # the before is ready on its first read: no wait before it

    def read_cheap(self) -> Observed:
        return NAVIGATED if self._clock.t >= self._at else CONTENT


def test_a_small_early_change_holds_to_the_guard() -> None:
    """Live run 2: 13 of 24 samples ended here at 47-109 ms and navigated later."""
    row, browser = _sample(CONTENT, lambda: NAVIGATED)
    assert row["timed_out"] is True
    assert row["conclusive_ms"] is None
    assert row["weak_reads"] >= 1
    assert browser.v2_reads[1] - browser.v2_reads[0] >= SUBMIT_GUARD_S + LATE_S


def test_a_navigation_after_a_small_change_is_waited_for_and_its_arrival_logged() -> None:
    clock = Clock()
    browser = LateNavigation(clock, after_s=3.0)
    row = site_sample(browser, RESULTS_URL, sleep=clock.sleep, clock=clock.now)
    assert row["classes"] == ["navigated"]
    assert row["timed_out"] is False
    assert 3000 <= row["conclusive_ms"] <= 3100
    assert row["late_changed"] is False


def test_an_immediate_navigation_arrives_at_the_first_poll() -> None:
    row, _ = _sample(NAVIGATED, lambda: NAVIGATED)
    assert row["conclusive_ms"] == 0
    assert row["timed_out"] is False


# ---- [V2.42] (B), mdn1: the before is read only once the start page is ready -------------------

FRAME_ONLY = _obs([(8, "Pane", "Tabs", (9, 88), None)])
"""A browser frame with no Document: the start page has not loaded yet (mdn1's before)."""


class SlowStart(FakeBrowser):
    """`read_v2` gives a frame with no Document `unready` times, then the ready start page."""

    def __init__(self, clock: Clock, unready: int) -> None:
        super().__init__(clock, NAVIGATED, lambda: NAVIGATED)
        self._unready = unready

    def read_v2(self) -> Observed:
        self.v2_reads.append(self._clock.t)
        if len(self.v2_reads) <= self._unready:
            return FRAME_ONLY
        return BEFORE if len(self.v2_reads) == self._unready + 1 else self._late()


def test_the_before_is_read_again_until_the_start_page_has_a_document() -> None:
    clock = Clock()
    browser = SlowStart(clock, unready=2)
    row = site_sample(browser, RESULTS_URL, sleep=clock.sleep, clock=clock.now)
    assert row["before_unready"] is False
    assert row["before_top_document"] is True
    assert row["before_stable"] is True
    assert row["classes"] == ["navigated"]
    assert browser.v2_reads[2] > 0.0  # waited between the reads, never a fixed sleep first


def test_a_start_page_that_never_gets_a_document_is_before_unready_never_a_verdict() -> None:
    clock = Clock()
    browser = SlowStart(clock, unready=10_000)
    row = site_sample(browser, RESULTS_URL, sleep=clock.sleep, clock=clock.now)
    assert row == {"before_unready": True, "before_top_document": False, "before_stable": True}
    assert browser.scripts == []  # never navigated, never watched
    assert clock.t <= SITE_READY_S + 1.0  # bounded by the guard


def test_the_row_records_the_before_and_after_facts() -> None:
    row, browser = _sample(NAVIGATED, lambda: NAVIGATED)
    assert (row["before_unready"], row["before_top_document"], row["before_stable"]) == (
        False,
        True,
        True,
    )
    assert row["after_minimized"] is False
    assert browser.v2_reads[0] == 0.0  # a ready start page is read at once
