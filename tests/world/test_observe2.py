"""The v2 observe path (step 3b, [V2.5]): v1's snapshot plus separate reads; a failed read is
None (not observed), the value is kept only as a `ValueFact`, and a secure field is never read."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

from steerai.world.address_bar import DocumentUrl
from steerai.world.effects import Observed, value_fact
from steerai.world.landmarks import Landmark, PageFacts
from steerai.world.model import Snapshot
from steerai.world.observe2 import (
    FIRST_QUIET_S,
    FirstTouches,
    document_nodes,
    first_observation,
    in_place_url,
    read_observed,
)
from tests.world.test_effects_conditions import BASE, PAGE, _snap

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


# ---- [V2.28] 2: first-observation readiness, keyed on the first touch of a process ----------

EMPTY_PAGE = _snap([PAGE])
"""A fresh Chrome's first read: the Document exists, its tree is not built yet."""
NO_DOCUMENT = _snap([(1, "Pane", "Chrome", (7, 1), None), (2, "Button", "Back", (7, 2), 1)])
"""A fresh Chrome before its Document appears (measured up to 3 s after the first touch), or a
native window, which never has one."""
GROWN = _snap([*BASE, (5, "Edit", "Email", (9, 5), 1)])
"""The same page one node bigger: a lazily built tree still growing."""
PID = 4242


def _pid(snapshot: Snapshot, pid: int = PID) -> Snapshot:
    return dataclasses.replace(snapshot, pid=pid)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_document_nodes_are_the_descendants_of_the_top_document() -> None:
    assert document_nodes(_snap(BASE)) == frozenset({2, 3, 4})
    assert document_nodes(EMPTY_PAGE) == frozenset()
    assert document_nodes(NO_DOCUMENT) is None


def test_the_first_touch_is_never_ready_even_on_a_built_page() -> None:
    """The quiet window starts after the first touch: one read cannot show it."""
    touches = FirstTouches(clock=_Clock().monotonic)
    assert touches.ready(_pid(_snap(BASE))) is False


def test_a_built_document_is_ready_once_its_node_count_holds_for_the_quiet_window() -> None:
    clock = _Clock()
    touches = FirstTouches(clock=clock.monotonic)
    touches.ready(_pid(_snap(BASE)))
    clock.now = FIRST_QUIET_S - 0.01
    assert touches.ready(_pid(_snap(BASE))) is False
    clock.now = FIRST_QUIET_S
    assert touches.ready(_pid(_snap(BASE))) is True


def test_a_growing_document_restarts_the_quiet_window() -> None:
    """gh1: a 156-node plateau held up to 2.5 s before the page grew to 303 nodes."""
    clock = _Clock()
    touches = FirstTouches(clock=clock.monotonic)
    touches.ready(_pid(_snap(BASE)))
    clock.now = FIRST_QUIET_S - 0.5
    assert touches.ready(_pid(GROWN)) is False
    clock.now = FIRST_QUIET_S + 0.5
    assert touches.ready(_pid(GROWN)) is False
    clock.now = 2 * FIRST_QUIET_S - 0.5
    assert touches.ready(_pid(GROWN)) is True


def test_a_document_with_no_children_is_never_ready() -> None:
    clock = _Clock()
    touches = FirstTouches(clock=clock.monotonic)
    for step in range(5):
        clock.now = step * FIRST_QUIET_S
        assert touches.ready(_pid(EMPTY_PAGE)) is False


def test_an_unstable_snapshot_is_not_ready() -> None:
    clock = _Clock()
    touches = FirstTouches(clock=clock.monotonic)
    touches.ready(_pid(_snap(BASE)))
    clock.now = FIRST_QUIET_S
    assert touches.ready(_pid(_snap(BASE, stable=False))) is False


def test_a_window_with_no_document_is_ready_when_its_node_count_holds() -> None:
    """A native window never has a Document; its whole tree must hold for the quiet window."""
    clock = _Clock()
    touches = FirstTouches(clock=clock.monotonic)
    touches.ready(_pid(NO_DOCUMENT))
    clock.now = FIRST_QUIET_S
    assert touches.ready(_pid(NO_DOCUMENT)) is True


def test_a_document_appearing_restarts_the_quiet_window() -> None:
    clock = _Clock()
    touches = FirstTouches(clock=clock.monotonic)
    touches.ready(_pid(NO_DOCUMENT))
    clock.now = FIRST_QUIET_S - 0.25
    assert touches.ready(_pid(_snap(BASE))) is False
    clock.now = FIRST_QUIET_S + 0.25
    assert touches.ready(_pid(_snap(BASE))) is False


def test_readiness_is_keyed_on_the_process_not_the_browser_name() -> None:
    """A second process of the same browser is its own first touch; a ready process stays
    ready for the rest of the task, whatever its tree does next."""
    clock = _Clock()
    touches = FirstTouches(clock=clock.monotonic)
    touches.ready(_pid(_snap(BASE)))
    clock.now = FIRST_QUIET_S
    assert touches.ready(_pid(_snap(BASE))) is True
    assert touches.ready(_pid(_snap(BASE), pid=PID + 1)) is False
    assert touches.ready(_pid(EMPTY_PAGE)) is True


def _reader(snapshots: list[Snapshot]) -> tuple[list[int], Callable[[], Observed]]:
    calls: list[int] = []

    def read() -> Observed:
        calls.append(1)
        return read_observed(snapshots[min(len(calls), len(snapshots)) - 1], Reader(), key=KEY)

    return calls, read


def test_the_first_observation_polls_until_the_document_holds() -> None:
    clock = _Clock()
    calls, read = _reader([_pid(EMPTY_PAGE), _pid(_snap(BASE))])
    touches = FirstTouches(clock=clock.monotonic)
    got = first_observation(read, touches=touches, clock=clock.monotonic, sleep=clock.sleep)
    assert got.snapshot.stable is True
    assert len(got.snapshot.elements) == len(BASE)
    assert clock.now >= FIRST_QUIET_S
    assert len(calls) > 2


def test_a_later_observation_of_a_ready_process_does_not_wait() -> None:
    clock = _Clock()
    touches = FirstTouches(clock=clock.monotonic)
    _, read = _reader([_pid(_snap(BASE))])
    first_observation(read, touches=touches, clock=clock.monotonic, sleep=clock.sleep)
    calls, again = _reader([_pid(_snap(BASE))])
    first_observation(again, touches=touches, clock=clock.monotonic, sleep=clock.sleep)
    assert len(calls) == 1


def test_the_first_observation_ends_unstable_at_the_deadline() -> None:
    clock = _Clock()
    calls, read = _reader([_pid(EMPTY_PAGE)])
    got = first_observation(
        read,
        touches=FirstTouches(clock=clock.monotonic),
        clock=clock.monotonic,
        sleep=clock.sleep,
        deadline_s=2.0,
        poll_s=0.5,
    )
    assert got.snapshot.stable is False, "never handed over as a stable, partial listing"
    assert 3 <= len(calls) <= 6


def test_a_page_still_growing_at_the_deadline_is_unstable() -> None:
    """A model-stable but growing tree (the RuntimeId set repeated twice) is not handed over."""
    clock = _Clock()
    pages = [_pid(_snap(BASE)) if step % 2 else _pid(GROWN) for step in range(100)]
    _, read = _reader(pages)
    touches = FirstTouches(clock=clock.monotonic)
    got = first_observation(read, touches=touches, clock=clock.monotonic, sleep=clock.sleep)
    assert got.snapshot.stable is False


def test_without_touches_the_call_is_the_first_touch() -> None:
    clock = _Clock()
    _, read = _reader([_pid(_snap(BASE))])
    got = first_observation(read, clock=clock.monotonic, sleep=clock.sleep)
    assert got.snapshot.stable is True
    assert clock.now >= FIRST_QUIET_S


def test_reads_do_not_apply_the_readiness_rule() -> None:
    """[V2.28] 2 replaces #305's per-read test: readiness is the first observation's alone."""
    assert read_observed(EMPTY_PAGE, Reader(), key=KEY).snapshot.stable is True
