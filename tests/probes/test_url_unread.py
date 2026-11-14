"""[V2.28] (b): why a site sample's in-place URL is unread, as one closed cause per sample.
`in_place_url` gives None with `url_observed` true for four reasons the row could not tell
apart; the cause says which. Fakes only."""

from __future__ import annotations

from typing import Any

from steerai.probes.effect_pages import site_sample
from steerai.probes.url_unread import read_bar, url_cause, url_fields
from steerai.world.effects import Observed
from tests.probes.test_site_late_read import (
    NAVIGATED,
    RESULTS_URL,
    Clock,
    FakeBrowser,
)
from tests.world.test_effects_conditions import BASE, _snap


def _after(
    *, url: str | None, document: str | None, observed: bool = True, stable: bool = True
) -> Observed:
    return Observed(
        snapshot=_snap(BASE, stable=stable),
        url=url,
        windows=(16,),
        document_url=document,
        url_observed=observed,
    )


def test_a_read_url_has_no_cause() -> None:
    after = _after(url="https://a.test/x", document="https://a.test/")
    assert url_cause(after, "a.test/x", bar_error=False) == "read"


def test_no_bar_is_not_observed() -> None:
    after = _after(url=None, document="https://a.test/", observed=False)
    assert url_cause(after, None, bar_error=False) == "no_bar"


def test_a_document_url_unread_is_told_from_a_host_mismatch() -> None:
    gone = _after(url=None, document=None)
    assert url_cause(gone, "a.test/x", bar_error=False) == "document_url_unread"
    other = _after(url=None, document="https://a.test/")
    assert url_cause(other, "b.test/x", bar_error=False) == "host_mismatch"


def test_a_bar_that_fails_when_re_read_is_a_bar_error() -> None:
    other = _after(url=None, document="https://a.test/")
    assert url_cause(other, None, bar_error=True) == "bar_error"


def test_a_www_and_a_punycode_host_are_told_apart() -> None:
    www = _after(url=None, document="https://www.a.test/")
    assert url_cause(www, "a.test/x", bar_error=False) == "readable_now", "the bar elides www"
    puny = _after(url=None, document="https://xn--bcher-kva.test/")
    assert url_cause(puny, "bücher.test/x", bar_error=False) == "host_mismatch"


def test_the_fields_carry_the_watch_end_state_not_the_late_one() -> None:
    after = _after(url=None, document=None, stable=False)
    fields = url_fields(after, "a.test/x", bar_error=False)
    assert fields["url_cause"] == "document_url_unread"
    assert fields["document_url_read"] is False
    assert fields["after_stable"] is False
    assert fields["document_children"] == 2
    assert "a.test" not in str(fields)


def test_read_bar_says_an_error_without_raising() -> None:
    class Broken:
        def address_bar(self, hwnd: int) -> str | None:  # noqa: ARG002 - the reader's shape
            raise OSError("gone")

    assert read_bar(Broken(), 1) == (None, True)


def test_the_site_sample_row_carries_the_cause() -> None:
    clock = Clock()
    browser = FakeBrowser(clock, NAVIGATED, lambda: NAVIGATED)
    row: dict[str, Any] = site_sample(browser, RESULTS_URL, sleep=clock.sleep, clock=clock.now)
    assert row["url_cause"] in {
        "read",
        "readable_now",
        "document_url_unread",
        "host_mismatch",
        "no_bar",
    }
    assert {"document_url_read", "after_stable", "document_children"} <= set(row)
