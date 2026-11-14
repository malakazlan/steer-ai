"""The v2 observe path (step 3b, [V2.5]): v1's snapshot plus separate reads; a failed read is
None (not observed), the value is kept only as a `ValueFact`, and a secure field is never read."""

from __future__ import annotations

import dataclasses
from typing import Any

from steerai.world.address_bar import DocumentUrl
from steerai.world.effects import Observed, value_fact
from steerai.world.landmarks import Landmark, PageFacts
from steerai.world.observe2 import (
    GROW_GUARD_S,
    GROW_POLL_S,
    DocumentSince,
    FirstTouches,
    document_nodes,
    first_observation,
    hold_still,
    in_place_url,
    read_observed,
)
from tests.world.test_effects_conditions import BASE, PAGE, Row, _snap

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

    snap = dataclasses.replace(_snap(BASE), unread_secure=frozenset({2}))
    seen = read_observed(snap, Reader(), key=KEY, acted=2, acted_value=_never)
    assert seen.acted_value is None


def test_the_value_of_a_secure_field_is_never_read() -> None:

    snap = _snap(BASE)
    secure = [dataclasses.replace(e, secure=True) if e.id == 2 else e for e in snap.elements]
    seen = read_observed(
        dataclasses.replace(snap, elements=secure), Reader(), key=KEY, acted=2, acted_value=_never
    )
    assert seen.acted_value is None


def test_the_value_of_a_read_non_secure_field_is_still_read() -> None:
    seen = read_observed(_snap(BASE), Reader(), key=KEY, acted=2, acted_value=lambda: "red")
    assert seen.acted_value == value_fact("red", key=KEY)


# ---- [V2.31] first-observation readiness: hand over early, hold while the page grows ----------

QUIET_S = 0.5
"""The loop's normal quiet window, as the caller passes it."""
NO_DOCUMENT: list[Row] = [(1, "Pane", "Chrome", (7, 1), None), (2, "Button", "Back", (7, 2), 1)]
"""A browser frame before its Document appears, or a native window: no Document either way."""
PID = 4242


def _page(nodes: int, *, document: tuple[int, ...] = (9, 1)) -> list[Row]:
    """A top Document with `nodes` elements under it."""
    rows: list[Row] = [(1, "Document", "Shop", document, None)]
    rows += [(i + 2, "ListItem", f"Item {i}", (9, 100 + i), 1) for i in range(nodes)]
    return rows


def _seen(rows: list[Row], *, bar: bool = True, stable: bool = True, pid: int = PID) -> Observed:
    """A full read: `bar` is whether the window has an address bar (`url_observed`)."""
    snapshot = dataclasses.replace(_snap(rows, stable=stable), pid=pid)
    return Observed(snapshot=snapshot, url_observed=bar)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _Screen:
    """Each frame is what a read sees from its time on; counts every read."""

    def __init__(self, clock: _Clock, frames: list[tuple[float, Observed]]) -> None:
        self.clock, self.frames, self.reads = clock, frames, 0

    def read(self) -> Observed:
        self.reads += 1
        return [seen for at, seen in self.frames if at <= self.clock.now][-1]


def _first(screen: _Screen, clock: _Clock, **kwargs: Any) -> Observed:  # Any: keyword knobs
    return first_observation(
        screen.read, quiet_s=QUIET_S, clock=clock.monotonic, sleep=clock.sleep, **kwargs
    )


def test_document_nodes_are_the_descendants_of_the_top_document() -> None:
    assert document_nodes(_snap(BASE)) == frozenset({2, 3, 4})
    assert document_nodes(_snap([PAGE])) == frozenset()
    assert document_nodes(_snap(NO_DOCUMENT)) is None


def test_an_observation_is_not_growing_unless_said() -> None:
    seen = Observed(snapshot=_snap(BASE))
    assert seen.growing is False
    assert seen.document_since is None


def test_a_settled_page_is_handed_over_after_one_normal_quiet_window() -> None:
    clock = _Clock()
    got = _first(_Screen(clock, [(0.0, _seen(_page(5)))]), clock)
    assert got.snapshot.stable is True
    assert got.growing is False
    assert QUIET_S <= clock.now < QUIET_S + 0.1, "the normal quiet window, not a long one"


def test_a_page_growing_in_steps_is_handed_over_with_the_token_and_a_later_read_sees_the_rest() -> (
    None
):
    """gh1: partial plateaus of 0.75-2.5 s before the page grew; the 2 s plateau is handed over
    early, marked growing; the rest arrives in a later observation."""
    clock = _Clock()
    screen = _Screen(
        clock,
        [
            (0.0, _seen(_page(3))),
            (0.2, _seen(_page(5))),
            (0.4, _seen(_page(8))),
            (2.4, _seen(_page(20))),
        ],
    )
    got = _first(screen, clock)
    assert len(document_nodes(got.snapshot) or ()) == 8
    assert got.growing is True
    assert got.snapshot.stable is True
    assert clock.now < 2.4, "handed over on the plateau, not after it"
    clock.now = 3.0
    assert len(document_nodes(screen.read().snapshot) or ()) == 20


def test_the_hold_polls_about_every_30_ms() -> None:
    clock = _Clock()
    screen = _Screen(clock, [(0.0, _seen(_page(3))), (0.1, _seen(_page(5)))])
    _first(screen, clock)
    assert screen.reads >= int((0.1 + QUIET_S) / GROW_POLL_S)
    assert GROW_POLL_S == 0.03


def test_a_page_that_never_stops_growing_is_handed_over_at_the_grow_guard() -> None:
    clock = _Clock()
    frames = [(step * 0.2, _seen(_page(3 + step))) for step in range(100)]
    got = _first(_Screen(clock, frames), clock)
    assert got.growing is True
    assert got.snapshot.stable is True
    assert GROW_GUARD_S <= clock.now < GROW_GUARD_S + 0.1
    assert GROW_GUARD_S == 3.0


def test_notepad_is_ready_at_once() -> None:
    """No Document and no address bar: a native window, handed over on its first stable read."""
    clock = _Clock()
    screen = _Screen(clock, [(0.0, _seen(NO_DOCUMENT, bar=False))])
    got = _first(screen, clock)
    assert got.snapshot.stable is True
    assert got.growing is False
    assert screen.reads == 1
    assert clock.now == 0.0


def test_a_browser_frame_with_no_document_is_not_handed_over() -> None:
    """No Document but an address bar: the page has not appeared; never a bare frame."""
    clock = _Clock()
    screen = _Screen(clock, [(0.0, _seen(NO_DOCUMENT))])
    got = _first(screen, clock, deadline_s=2.0)
    assert got.snapshot.stable is False
    assert clock.now >= 2.0


def test_a_browser_frame_waits_for_its_document_then_holds_it() -> None:
    clock = _Clock()
    screen = _Screen(clock, [(0.0, _seen(NO_DOCUMENT)), (1.5, _seen(_page(6)))])
    got = _first(screen, clock)
    assert len(document_nodes(got.snapshot) or ()) == 6
    assert got.snapshot.stable is True
    assert clock.now >= 1.5 + QUIET_S


def test_an_empty_document_is_not_handed_over() -> None:
    clock = _Clock()
    got = _first(_Screen(clock, [(0.0, _seen([PAGE]))]), clock, deadline_s=2.0)
    assert got.snapshot.stable is False


def test_an_unstable_read_is_not_handed_over_before_the_deadline() -> None:
    clock = _Clock()
    got = _first(_Screen(clock, [(0.0, _seen(_page(5), stable=False))]), clock, deadline_s=2.0)
    assert got.snapshot.stable is False
    assert clock.now >= 2.0


def test_a_ready_process_is_not_held_again_in_the_task() -> None:
    clock = _Clock()
    touches = FirstTouches()
    _first(_Screen(clock, [(0.0, _seen(_page(5)))]), clock, touches=touches)
    again = _Screen(clock, [(0.0, _seen(_page(9)))])
    _first(again, clock, touches=touches)
    assert again.reads == 1
    other = _Screen(clock, [(0.0, _seen(_page(5), pid=PID + 1))])
    _first(other, clock, touches=touches)
    assert other.reads > 1, "another process is its own first touch, whatever its name"


def test_hold_still_ends_at_its_guard_while_the_count_keeps_changing() -> None:
    """The loop's load-guard wait uses the same hold with its own guard."""
    clock = _Clock()
    frames = [(step * 0.2, _seen(_page(3 + step))) for step in range(100)]
    screen = _Screen(clock, frames)
    got = hold_still(
        screen.read,
        screen.read(),
        quiet_s=QUIET_S,
        guard_s=1.0,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    assert got.growing is True
    assert 1.0 <= clock.now < 1.1


def test_document_since_is_when_the_top_document_was_first_seen() -> None:
    clock = _Clock()
    since = DocumentSince(clock=clock.monotonic)
    clock.now = 1.0
    assert since(_seen(_page(3))).document_since == 1.0
    clock.now = 4.0
    assert since(_seen(_page(9))).document_since == 1.0, "the same Document, grown"
    clock.now = 6.0
    replaced = since(_seen(_page(2, document=(9, 77))))
    assert replaced.document_since == 6.0
    assert since(_seen(NO_DOCUMENT, bar=False)).document_since is None


def test_reads_do_not_apply_the_readiness_rule() -> None:
    """Readiness is the first observation's alone: a read never changes `stable`."""
    assert read_observed(_snap([PAGE]), Reader(), key=KEY).snapshot.stable is True
