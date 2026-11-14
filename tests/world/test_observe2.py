"""The v2 observe path (step 3b, [V2.5]): v1's snapshot plus separate reads; a failed read is
None (not observed), the value is kept only as a `ValueFact`, and a secure field is never read."""

from __future__ import annotations

from steerai.world.address_bar import DocumentUrl
from steerai.world.effects import Observed, observe, poll_tier, value_fact
from steerai.world.landmarks import Landmark, PageFacts
from steerai.world.observe2 import (
    document_ready,
    first_observation,
    in_place_url,
    poll_observed,
    read_observed,
)
from tests.world.test_effects_conditions import BASE, FIELD, PAGE, _snap

KEY = b"k" * 32


class Reader:
    def __init__(self, *, fail: frozenset[str] = frozenset()) -> None:
        self.fail = fail
        self.calls: list[str] = []
        self.hwnds: list[int] = []
        self.snapshots: list[object] = []

    def document_url_in(self, snapshot: object, document_id: int) -> DocumentUrl | None:
        self.snapshots.append(snapshot)
        self.calls.append("url")
        if "url" in self.fail:
            raise OSError("gone")
        return DocumentUrl(runtime_id=(9, document_id), url="https://shop.test/?q=red")

    def address_bar(self, hwnd: int) -> str | None:
        self.calls.append("bar")
        self.hwnds.append(hwnd)
        if "url" in self.fail:
            raise OSError("gone")
        return "shop.test/?q=red"

    def focused_runtime_id(self) -> tuple[int, ...]:
        self.calls.append("focus")
        if "focus" in self.fail:
            raise OSError("gone")
        return (9, 2)

    def page_facts(self, hwnd: int) -> PageFacts:
        self.calls.append("roles")
        self.hwnds.append(hwnd)
        if "roles" in self.fail:
            raise OSError("gone")
        return PageFacts(
            roles={(9, 2): "searchbox"},
            headings={(9, 4): 1},
            landmarks={(9, 2): Landmark("search", (9, 1)), (9, 4): None},
        )


def test_every_fact_is_read_and_the_value_is_only_a_fact() -> None:
    seen = read_observed(
        _snap(BASE), Reader(), key=KEY, windows=lambda: (16,), acted_value=lambda: "red shoes"
    )
    assert seen.url == "https://shop.test/?q=red"
    assert seen.focus == (9, 2)
    assert seen.windows == (16,)
    assert seen.aria_roles == {(9, 2): "searchbox"}
    assert seen.headings == {(9, 4): 1}
    assert seen.landmarks == {(9, 2): Landmark("search", (9, 1)), (9, 4): None}
    assert seen.acted_value == value_fact("red shoes", key=KEY)
    assert "red shoes" not in repr(seen)


def test_a_failed_read_is_none_never_a_guess() -> None:
    seen = read_observed(
        _snap(BASE),
        Reader(fail=frozenset({"url", "focus", "roles"})),
        key=KEY,
        windows=lambda: (16,),
    )
    assert (seen.url, seen.focus, seen.aria_roles, seen.headings) == (None, None, None, None)
    assert seen.landmarks is None, "not observed: the query conditions fail closed"


def test_no_acted_reader_means_no_value_read() -> None:
    seen = read_observed(_snap(BASE), Reader(), key=KEY)
    assert seen.acted_value is None
    assert seen.windows is None


def test_a_window_without_a_page_skips_the_page_reads() -> None:
    reader = Reader()
    seen = read_observed(_snap(BASE), reader, key=KEY, page_facts=False)
    assert reader.calls == ["focus"]
    assert seen.url is None
    assert seen.headings is None


# ---- [V2.7] the URL from two sources -------------------------------------------------------


class BarReader(Reader):
    def __init__(self, *, document: str | None, bar: str | None, **kw: frozenset[str]) -> None:
        super().__init__(**kw)
        self.document, self.bar = document, bar

    def document_url_in(self, snapshot: object, document_id: int) -> DocumentUrl | None:
        self.snapshots.append(snapshot)
        self.calls.append("document")
        if self.document is None:
            return None
        return DocumentUrl(runtime_id=(9, document_id), url=self.document)

    def address_bar(self, hwnd: int) -> str | None:
        self.calls.append("bar")
        self.hwnds.append(hwnd)
        return self.bar


def test_the_bar_gives_the_in_place_url_and_the_document_its_identity() -> None:
    reader = BarReader(document="https://shop.test/", bar="shop.test/?q=red+shoes")
    seen = read_observed(_snap(BASE), reader, key=KEY)
    assert seen.document_url == "https://shop.test/"
    assert seen.url == "https://shop.test/?q=red+shoes", "the Document's scheme, the bar's rest"
    assert seen.url_observed is True


def test_a_bar_on_another_host_makes_the_url_unread() -> None:
    reader = BarReader(document="https://shop.test/", bar="other.test/typed words")
    seen = read_observed(_snap(BASE), reader, key=KEY)
    assert seen.url is None
    assert seen.url_observed is True, "a mismatch is unread, never not observed"


def test_no_bar_means_the_in_place_url_is_not_observed() -> None:
    reader = BarReader(document="https://shop.test/", bar=None)
    seen = read_observed(_snap(BASE), reader, key=KEY)
    assert seen.document_url == "https://shop.test/"
    assert seen.url is None
    assert seen.url_observed is False


# ---- [V2.8] the bar's host --------------------------------------------------------------------


def test_a_bar_without_the_documents_www_is_the_same_host() -> None:
    url, observed = in_place_url("https://www.shop.test/", "shop.test/search?q=red", bar_read=True)
    assert url == "https://www.shop.test/search?q=red", "scheme and host from the Document"
    assert observed is True


def test_the_reverse_www_difference_is_unread() -> None:
    assert in_place_url("https://shop.test/", "www.shop.test/?q=red", bar_read=True) == (None, True)


def test_a_unicode_bar_host_against_a_punycode_document_fails_closed() -> None:
    document = "https://xn--bcher-kva.test/"
    assert in_place_url(document, "bücher.test/?q=red", bar_read=True) == (None, True)


# ---- [V2.26] (2): no 2b value read on a field whose secure flag is unread or set --------------


def _never() -> str:
    raise AssertionError("the value of this field must not be read")


def test_the_value_of_a_field_with_an_unread_secure_flag_is_never_read() -> None:
    import dataclasses  # noqa: PLC0415 - this test only

    snap = dataclasses.replace(_snap(BASE), unread_secure=frozenset({2}))
    seen = read_observed(snap, Reader(), key=KEY, acted=2, acted_value=_never)
    assert seen.acted_value is None


def test_the_value_of_a_secure_field_is_never_read() -> None:
    import dataclasses  # noqa: PLC0415 - this test only

    snap = _snap(BASE)
    secure = [dataclasses.replace(e, secure=True) if e.id == 2 else e for e in snap.elements]
    seen = read_observed(
        dataclasses.replace(snap, elements=secure), Reader(), key=KEY, acted=2, acted_value=_never
    )
    assert seen.acted_value is None


def test_the_value_of_a_read_non_secure_field_is_still_read() -> None:
    seen = read_observed(_snap(BASE), Reader(), key=KEY, acted=2, acted_value=lambda: "red")
    assert seen.acted_value == value_fact("red", key=KEY)


# ---- [V2.27] the first-observation rule: a scope Document with no descendants is not ready ----

EMPTY_PAGE = _snap([PAGE])
"""A fresh Chrome's first read: the Document exists, its tree is not built yet."""


def test_a_document_with_descendants_is_ready() -> None:
    assert document_ready(_snap(BASE)) is True


def test_a_document_with_no_descendants_is_not_ready() -> None:
    assert document_ready(EMPTY_PAGE) is False


def test_a_window_with_no_document_is_ready() -> None:
    """A native window has no Document: the rule is about a page whose tree is not built."""
    button = (1, "Button", "Save", (7, 1), None)
    assert document_ready(_snap([button])) is True


def test_the_full_read_of_an_empty_document_is_unstable() -> None:
    seen = read_observed(EMPTY_PAGE, Reader(), key=KEY)
    assert seen.snapshot.stable is False
    assert read_observed(_snap(BASE), Reader(), key=KEY).snapshot.stable is True


def test_a_poll_of_an_empty_document_is_unstable_and_reports_unknown() -> None:
    polled = poll_observed(EMPTY_PAGE)
    assert polled.tier == "poll"
    assert polled.snapshot.stable is False
    assert observe(poll_tier(_snap(BASE)), polled, acted=FIELD[3]).classes == {"unknown"}


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_the_first_observation_polls_until_the_document_is_built() -> None:
    clock = _Clock()
    reads = iter([EMPTY_PAGE, EMPTY_PAGE, _snap(BASE)])
    calls: list[int] = []

    def read() -> Observed:
        calls.append(1)
        return read_observed(next(reads), Reader(), key=KEY)

    got = first_observation(read, clock=clock.monotonic, sleep=clock.sleep)
    assert got.snapshot.stable is True
    assert len(got.snapshot.elements) == len(BASE)
    assert len(calls) == 3


def test_the_first_observation_ends_unstable_at_the_deadline() -> None:
    clock = _Clock()
    calls: list[int] = []

    def read() -> Observed:
        calls.append(1)
        return read_observed(EMPTY_PAGE, Reader(), key=KEY)

    got = first_observation(
        read, clock=clock.monotonic, sleep=clock.sleep, deadline_s=2.0, poll_s=0.5
    )
    assert got.snapshot.stable is False, "never handed over as a stable, empty listing"
    assert 3 <= len(calls) <= 6
