"""[V2.42] (B): every `unknown` the observer reports carries its cause as a token,
`unknown:<branch>`, in the normal report (not only in a debug run). The class stays `unknown`.
mdn1's root cause: the before read was taken before the start page had a Document."""

from __future__ import annotations

import dataclasses

from steerai.world.effects import Observed, observe
from steerai.world.watch import watch
from tests.world.test_effects_conditions import Row, _snap

WINDOW: Row = (0, "Window", "w", (1,), None)
BAR: Row = (1, "Pane", "bar", (2,), 0)
DOC: Row = (2, "Document", "d", (9,), 1)
TEXT: Row = (3, "Text", "t", (10,), 2)
FRAME = [WINDOW, BAR]
PAGE = [*FRAME, DOC, TEXT]


def _obs(rows: list[Row], url: str = "https://x/") -> Observed:
    return Observed(snapshot=_snap(rows), url=url, windows=(1,))


def test_a_before_with_no_document_is_unknown_with_its_cause() -> None:
    """synth.py's case: the before is a browser frame with no Document, the after has one."""
    report = observe(_obs(FRAME), _obs(PAGE, "https://x/a"))
    assert report.classes == frozenset({"unknown"})
    assert "unknown:top_doc_missing_before" in report.tokens


def test_an_after_with_no_document_is_unknown_with_its_cause() -> None:
    report = observe(_obs(PAGE), _obs(FRAME))
    assert report.classes == frozenset({"unknown"})
    assert "unknown:top_doc_missing_after" in report.tokens


def test_another_window_is_unknown_window_changed() -> None:
    other = dataclasses.replace(_obs(PAGE), snapshot=dataclasses.replace(_snap(PAGE), hwnd=99))
    report = observe(_obs(PAGE), other)
    assert report.classes == frozenset({"unknown"})
    assert "unknown:window_changed" in report.tokens


def test_an_unstable_after_is_unknown_unstable() -> None:
    shaky = Observed(snapshot=_snap(PAGE, stable=False), url="https://x/", windows=(1,))
    report = observe(_obs(PAGE), shaky)
    assert "unknown:unstable" in report.tokens


def test_a_minimized_after_is_unknown_minimized() -> None:
    small = dataclasses.replace(
        _obs(PAGE), snapshot=dataclasses.replace(_snap(PAGE), minimized=True)
    )
    report = observe(_obs(PAGE), small)
    assert "unknown:minimized" in report.tokens


def test_every_cause_that_holds_is_reported() -> None:
    both = Observed(
        snapshot=dataclasses.replace(_snap(PAGE, stable=False), minimized=True),
        url="https://x/",
        windows=(1,),
    )
    report = observe(_obs(PAGE), both)
    assert {"unknown:unstable", "unknown:minimized"} <= report.tokens


def test_a_report_that_is_not_unknown_carries_no_cause() -> None:
    report = observe(_obs(PAGE), _obs([*PAGE, (4, "Text", "new", (11,), 2)]))
    assert "unknown" not in report.classes
    assert not any(t.startswith("unknown:") for t in report.tokens)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def test_a_submit_watch_on_a_before_with_no_document_holds_to_the_guard() -> None:
    """The mdn1 pair: `unknown` is never conclusive, so the submit holds to its guard and the
    final report keeps the cause."""
    clock, after = _Clock(), _obs(PAGE, "https://x/a")
    watched = watch(
        lambda: after,
        _obs(FRAME),
        clock=clock.now,
        sleep=clock.sleep,
        poll_s=0.05,
        settle_s=0.3,
        deadline_s=1.0,
        full=lambda seen: seen,
        submit=True,
    )
    assert watched.timed_out
    assert watched.conclusive_ms is None
    assert "unknown:top_doc_missing_before" in watched.report.tokens
