"""[V2.28] (a): the per-site read-cost probe times each part of a full read, the page facts over
the whole window and scoped to the Document, with n >= 5 reads. Fakes only: no browser."""

from __future__ import annotations

import pytest

from steerai.probes.read_cost import measure_site, one_read, percentiles, summary
from steerai.world.address_bar import DocumentUrl
from steerai.world.landmarks import PageFacts
from steerai.world.model import Snapshot
from tests.world.test_effects_conditions import BASE, _snap

RID = (9, 1)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t


class FakeReads:
    """Each read advances the clock by its own cost; `calls` is the order they ran in."""

    def __init__(self, clock: Clock, snapshot: Snapshot | None = None) -> None:
        self.clock = clock
        self.snapshot = snapshot if snapshot is not None else _snap(BASE)
        self.calls: list[str] = []
        self.costs = {
            "snapshot": 0.05,
            "document_url": 0.002,
            "address_bar": 0.01,
            "focus": 0.001,
            "page_facts": 3.0,
            "page_facts_scoped": 0.4,
        }
        self.scoped = PageFacts(roles={RID: "main"}, headings={}, landmarks={})
        self.whole = PageFacts(roles={RID: "main", (9, 99): "banner"}, headings={}, landmarks={})
        self.fail: set[str] = set()

    def _run(self, name: str) -> None:
        self.calls.append(name)
        self.clock.t += self.costs[name]
        if name in self.fail:
            raise OSError(name)

    def observe(self) -> Snapshot:
        self._run("snapshot")
        return self.snapshot

    def document_url_in(self, snapshot: Snapshot, document_id: int) -> DocumentUrl | None:  # noqa: ARG002
        self._run("document_url")
        return DocumentUrl(runtime_id=RID, url="https://x.test/")

    def address_bar(self) -> str | None:
        self._run("address_bar")
        return None

    def focused_runtime_id(self) -> tuple[int, ...]:
        self._run("focus")
        return ()

    def page_facts(self) -> PageFacts:
        self._run("page_facts")
        return self.whole

    def page_facts_in(self, snapshot: Snapshot, document_id: int) -> PageFacts:  # noqa: ARG002
        self._run("page_facts_scoped")
        return self.scoped


def test_percentiles_use_the_probe_convention() -> None:
    assert percentiles([5, 1, 3, 2, 4]) == {"n": 5, "p50": 3, "p95": 5, "max": 5}


def test_one_read_times_every_part_and_compares_the_scoped_facts() -> None:
    clock = Clock()
    row = one_read(FakeReads(clock), flip=False, clock=clock.now)
    assert set(row["ms"]) == {
        "snapshot",
        "document_url",
        "address_bar",
        "focus",
        "page_facts",
        "page_facts_scoped",
    }
    assert row["ms"]["page_facts"] == pytest.approx(3000.0)
    assert row["ms"]["page_facts_scoped"] == pytest.approx(400.0)
    assert row["scoped_equal"] is True
    assert row["failed"] == []


def test_the_two_page_facts_swap_places_on_alternate_reads() -> None:
    clock = Clock()
    a, b = FakeReads(clock), FakeReads(clock)
    one_read(a, flip=False, clock=clock.now)
    one_read(b, flip=True, clock=clock.now)
    assert a.calls[-2:] == ["page_facts", "page_facts_scoped"]
    assert b.calls[-2:] == ["page_facts_scoped", "page_facts"]


def test_a_scoped_fact_that_disagrees_with_the_whole_window_is_reported() -> None:
    clock = Clock()
    reads = FakeReads(clock)
    reads.scoped = PageFacts(roles={RID: "navigation"}, headings={}, landmarks={})
    assert one_read(reads, flip=False, clock=clock.now)["scoped_equal"] is False


def test_a_failed_part_is_named_and_not_timed_into_the_percentiles() -> None:
    clock = Clock()
    reads = FakeReads(clock)
    reads.fail = {"page_facts"}
    site = measure_site("t1", reads, n=5, clock=clock.now)
    assert site["failed"] == ["page_facts"]
    assert "page_facts" not in site["parts"]
    assert site["parts"]["page_facts_scoped"]["n"] == 5


def test_a_site_needs_at_least_five_reads() -> None:
    clock = Clock()
    with pytest.raises(ValueError, match="at least 5"):
        measure_site("t1", FakeReads(clock), n=4, clock=clock.now)


def test_a_site_reports_p50_p95_per_part_and_the_whole_read_with_and_without_scoping() -> None:
    clock = Clock()
    site = measure_site("t1", FakeReads(clock), n=6, clock=clock.now)
    assert site["reads"] == 6
    assert site["parts"]["page_facts"]["p50"] == pytest.approx(3000.0, abs=0.1)
    assert site["full"]["p50"] > site["full_scoped"]["p50"]
    assert site["full"]["p50"] == pytest.approx(3063.0, abs=0.5)
    assert site["full_scoped"]["p50"] == pytest.approx(463.0, abs=0.5)
    assert site["scoped_equal"] is True
    assert site["scoped_compared"] == 6


def test_a_window_without_a_document_has_no_scoped_read() -> None:
    clock = Clock()
    reads = FakeReads(clock, _snap([]))
    row = one_read(reads, flip=False, clock=clock.now)
    assert "page_facts_scoped" not in row["ms"]
    assert "document_url" not in row["ms"]
    assert row["scoped_equal"] is None


def test_the_summary_has_one_line_per_site_and_names_an_error_row() -> None:
    clock = Clock()
    rows = [
        measure_site("t1", FakeReads(clock), n=5, clock=clock.now),
        {"task": "t2", "error": "TimeoutError"},
    ]
    lines = summary(rows)
    assert lines[0].startswith("t1: elements")
    assert lines[1] == "t2: error TimeoutError"
