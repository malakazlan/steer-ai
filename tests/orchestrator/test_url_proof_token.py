"""The address-bar proof's leg as a closed token (V2 step 1 packet §C; principal items 2 and
re-review 2; hunter OB-17/24/25): `url_proof` on every R2/R3 step whose eligibility was read,
so a miss says which leg failed. A new key: `detail` is never touched (the golden fixture pins
it), and every value is a fixed literal, never a URL, a title or the words."""

from __future__ import annotations

from typing import Any

import pytest

from steerai.orchestrator.loop import URL_LEGS
from tests.orchestrator.test_loop import HELLO, Harness, _type
from tests.orchestrator.test_url_proof_any_expect import _guessed
from tests.orchestrator.test_url_query_loop import HOME, RESULTS, Bar, _harness, _submit


def _leg(bar: Bar, step: Any = None, **kw: Any) -> dict[str, Any]:
    h = _harness(bar, **kw)
    return dict(h.session.run_step(step or _submit()).evidence)


@pytest.mark.parametrize(
    ("label", "bar", "kw", "leg"),
    [
        ("the words and a named load", Bar(HOME, RESULTS), {}, "counted"),
        ("an address bar the words never reached", Bar(HOME, HOME), {}, "no_words"),
        ("no before URL", Bar(None, RESULTS), {}, "no_url_before"),
        ("no URL after the Enter", Bar(HOME, None), {}, "no_url_after"),
        ("the words but no page for them", Bar(HOME, RESULTS), {"loaded": "Bing"}, "no_load"),
        ("not chrome", Bar(HOME, RESULTS), {"image": "notepad.exe"}, "not_eligible"),
    ],
)
def test_each_leg_is_named(label: str, bar: Bar, kw: dict[str, Any], leg: str) -> None:
    evidence = _leg(bar, **kw)
    assert evidence["url_proof"] == leg, label


def _new_page(bar: Bar) -> Any:
    """The Enter loads a new top Document named exactly the words: the expect itself holds."""
    h = _harness(bar)

    def press(_executor: Any, action: Any) -> None:
        if action.verb == "press":
            bar.pressed = True
            h.world.rows[:] = [r for r in h.world.rows if r[2] != "Bing"]
            h.world.rows.append((41, "Document", HELLO, (42, 91), 0))

    h.physical.on_execute = press
    return h.session.run_step(_submit())


def test_an_expect_that_held_never_ran_the_proof() -> None:
    result = _new_page(Bar(HOME, RESULTS))
    assert result.ok is True, result.to_dict()
    assert result.evidence["url_proof"] == "not_run"
    assert "url_reads" not in result.evidence


def test_a_page_titled_without_the_words_is_no_named_load() -> None:
    from steerai.world.address_bar import DocumentUrl  # noqa: PLC0415 - the read's shape

    bar = Bar(HOME, RESULTS)
    h = _harness(bar, loaded="Search results | Bing")
    h.session._document_url = lambda _hwnd: DocumentUrl(
        runtime_id=(42, 90), url="https://" + (RESULTS if bar.pressed else HOME)
    )
    assert h.session.run_step(_guessed()).evidence["url_proof"] == "no_named_load"


class Fading(Bar):
    """The words reach the bar, then the bar stops reading (focused, or mid-navigation)."""

    def __call__(self, hwnd: int) -> str | None:
        self.reads.append(hwnd)
        if not self.pressed:
            return self.before
        return self.after if len(self.reads) <= 2 else None


def test_the_leg_is_the_furthest_any_read_reached_not_the_last() -> None:
    evidence = _leg(Fading(HOME, RESULTS), _guessed(), loaded="Bing")
    assert evidence["url_proof"] == "no_load", "a later unreadable bar never hides the load miss"
    assert evidence["url_reads"] >= 2


def test_the_proof_counts_its_reads_when_it_ran() -> None:
    assert _leg(Bar(HOME, RESULTS))["url_reads"] >= 1


def test_a_step_below_r2_never_carries_the_token() -> None:
    h = Harness()
    assert "url_proof" not in h.session.run_step(_type()).evidence


def test_every_value_is_a_closed_literal() -> None:
    assert set(URL_LEGS) == {
        "not_eligible",
        "not_run",
        "no_url_before",
        "no_url_after",
        "no_words",
        "no_load",
        "no_named_load",
        "counted",
    }
