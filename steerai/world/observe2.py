"""The v2 observe path (V2 step 3b, [V2.5], [V2.7]): one `Observed` from the snapshot v1 already
fetches plus separate reads for the facts the observer needs. v1's fetch is unchanged; each
extra read is its own call, and a read that fails is `None` (not observed or unread), never a
guess.

- `document_url`: the top Document's URL through the snapshot's own handle ([V2.7]: identity,
  scheme and host, for `navigated` and error pages; correct after a real navigation).
- `url`: the in-place URL from the address bar (path, query, fragment), with the Document's
  scheme, accepted only when its host matches the Document's ([V2.7]); a mismatch is unread,
  a window without a bar has `url_observed=False`. Chrome 157 elides the scheme (probe #271).
- `windows`: the top-level window list.
- `focus`: the RuntimeId with keyboard focus.
- `acted_value`: the acted field's value as a `ValueFact` (filled + keyed digest); the text is
  dropped here and never kept. A secure field is never read: the caller passes no reader.
- `headings`, `aria_roles`, `landmarks`: one cached fetch of RuntimeId, AriaRole, HeadingLevel
  and LandmarkType ([V2.13]: each element's nearest landmark ancestor).
"""

from __future__ import annotations

import dataclasses
import time
import urllib.parse
from collections.abc import Callable
from typing import Literal, Protocol

from steerai.world.address_bar import DocumentUrl
from steerai.world.effects import Observed, top_document, value_fact
from steerai.world.landmarks import PageFacts
from steerai.world.model import Snapshot
from steerai.world.reads import ReadMeter

FIRST_READY_S = 10.0
"""[V2.27] how long a first observation waits for a page to appear (a browser frame with no
Document, or a Document with no children); a guard, unmeasured. Past it the last read carries
the unstable token."""
FIRST_POLL_S = 0.25
GROW_POLL_S = 0.03
"""[V2.31] the hold's poll: about 30 ms between reads (a read on a real page takes longer)."""
GROW_GUARD_S = 3.0
"""[V2.31] how long a first observation holds a page whose node count keeps changing before it
is handed over marked `growing`; provisional. Measured (first_observation --measure, gh1,
2026-11-14): partial plateaus of 0.75-2.5 s and a page still loading for up to 12.3 s, so no
hold tells a loading page from a built one; an observation can be partial and the loop handles
it (re-observe, the load guard), not this wait."""


def document_nodes(snapshot: Snapshot) -> frozenset[int] | None:
    """[V2.31] the ids of every element under the top Document (the Document itself not
    counted); None when the window has no top Document."""
    top = top_document(snapshot)
    if top is None:
        return None
    children: dict[int | None, list[int]] = {}
    for element in snapshot.elements:
        children.setdefault(element.parent_id, []).append(element.id)
    found: set[int] = set()
    pending = list(children.get(top.id, []))
    while pending:
        current = pending.pop()
        if current not in found:
            found.add(current)
            pending.extend(children.get(current, []))
    return frozenset(found)


def _count(snapshot: Snapshot) -> int:
    """What the hold watches: the top Document's node count, or the whole window's when it has
    no Document."""
    inside = document_nodes(snapshot)
    return snapshot.node_count if inside is None else len(inside)


def _shape(seen: Observed) -> Literal["page", "native", "waiting"]:
    """[V2.31] `page`: a Document with children; `native`: no Document and no address bar;
    `waiting`: a Document with no children, or no Document in a window with an address bar
    (`url_observed`), whose page has not appeared yet."""
    inside = document_nodes(seen.snapshot)
    if inside is None:
        return "waiting" if seen.url_observed else "native"
    return "page" if inside else "waiting"


def hold_still(  # noqa: PLR0913 - the read, the first read and the injected clock, keyword-only
    read: Callable[[], Observed],
    first: Observed,
    *,
    quiet_s: float,
    guard_s: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_s: float = GROW_POLL_S,
) -> Observed:
    """[V2.31] from `first`, read every `poll_s` until the node count (`_count`) has held for
    `quiet_s` on stable reads, or `guard_s` has passed. The last read is returned, `growing`
    when the count rose at any point. The first observation holds with `GROW_GUARD_S`; the
    loop's load-guard wait may hold with its own guard.

    Slow reads ([V2.34]): times are read completions; the quiet window counts only when two
    reads span it (`first` or the read that last changed, and a later one); the guard never
    ends on a change no later read has seen (one more read, once). `read_cost` logs this hold's
    reads (not `first`)."""
    if quiet_s <= 0 or guard_s < 0 or poll_s <= 0:
        raise ValueError("hold_still needs a positive quiet window and poll and a guard >= 0")
    meter = ReadMeter(clock)
    start = clock()
    seen, count, quiet_from, grew = first, _count(first.snapshot), start, False
    spanning, reread = 1, False
    while True:
        now = clock()
        quiet = seen.snapshot.stable and spanning >= 2 and now - quiet_from >= quiet_s
        if now - start >= guard_s and not quiet and spanning < 2 and not reread:
            reread = True
        elif quiet or now - start >= guard_s:
            return dataclasses.replace(seen, growing=grew, read_cost=meter.cost())
        sleep(poll_s)
        seen = meter.timed(read)
        current = _count(seen.snapshot)
        if current != count or not seen.snapshot.stable:
            grew = grew or current > count
            count, quiet_from, spanning = current, clock(), 1
        else:
            spanning += 1


class FirstTouches:
    """[V2.31] the processes whose first observation in this task was handed over, keyed on the
    pid (the window handle when the pid was not read), never the browser's name. One per
    task."""

    def __init__(self) -> None:
        self._ready: set[tuple[str, int]] = set()

    @staticmethod
    def _key(snapshot: Snapshot) -> tuple[str, int]:
        return ("pid", snapshot.pid) if snapshot.pid else ("hwnd", snapshot.hwnd)

    def ready(self, snapshot: Snapshot) -> bool:
        return self._key(snapshot) in self._ready

    def mark(self, snapshot: Snapshot) -> None:
        self._ready.add(self._key(snapshot))


def first_observation(  # noqa: PLR0913 - the read, the quiet window, the touches and the clock
    read: Callable[[], Observed],
    *,
    quiet_s: float,
    touches: FirstTouches | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    deadline_s: float = FIRST_READY_S,
    poll_s: float = FIRST_POLL_S,
    grow_guard_s: float = GROW_GUARD_S,
) -> Observed:
    """[V2.31] the first observation of a process's window in a task; `read` is a full v2 read
    and `quiet_s` the loop's normal quiet window. A process already handed over in this task
    gets its first read.

    - A native window (no Document, no address bar) is handed over on its first stable read.
    - A page (a Document with children) is held (`hold_still`) for one quiet window, longer
      while it grows, at most `grow_guard_s`; it may be handed over partial, marked `growing`.
    - Anything else (a Document with no children, a browser frame with no Document, an unstable
      read) is polled until `deadline_s`, then returned with the unstable token: never a bare
      browser frame. A hold that starts before the deadline may end up to `grow_guard_s` past it.

    Without `touches` this call is the process's first touch. `read_cost` logs every read
    this call made, by tier ([V2.34])."""
    touches = FirstTouches() if touches is None else touches
    meter = ReadMeter(clock)

    def timed() -> Observed:
        return meter.timed(read)

    deadline = clock() + deadline_s
    while True:
        seen = timed()
        if touches.ready(seen.snapshot):
            return dataclasses.replace(seen, read_cost=meter.cost())
        shape = _shape(seen) if seen.snapshot.stable else "waiting"
        if shape == "page":
            seen = hold_still(
                timed, seen, quiet_s=quiet_s, guard_s=grow_guard_s, clock=clock, sleep=sleep
            )
            shape = _shape(seen) if seen.snapshot.stable else "waiting"
        if shape != "waiting":
            touches.mark(seen.snapshot)
            return dataclasses.replace(seen, read_cost=meter.cost())
        if clock() >= deadline:
            unstable = dataclasses.replace(seen.snapshot, stable=False)
            return dataclasses.replace(seen, snapshot=unstable, read_cost=meter.cost())
        sleep(poll_s)


class DocumentSince:
    """[V2.31] stamps `Observed.document_since`: when this window's current top Document (by
    RuntimeId) was first seen through this stamp, on `clock`. A replaced Document restarts it.
    For the loop's load guard (give up only on a settled page)."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._seen: dict[int, tuple[tuple[int, ...], float]] = {}

    def __call__(self, seen: Observed) -> Observed:
        top = top_document(seen.snapshot)
        if top is None:
            return dataclasses.replace(seen, document_since=None)
        known = self._seen.get(seen.snapshot.hwnd)
        if known is None or known[0] != top.runtime_id:
            known = (top.runtime_id, self._clock())
            self._seen[seen.snapshot.hwnd] = known
        return dataclasses.replace(seen, document_since=known[1])


class FactReader(Protocol):
    def document_url_in(self, snapshot: Snapshot, document_id: int) -> DocumentUrl | None: ...

    def address_bar(self, hwnd: int) -> str | None: ...

    def focused_runtime_id(self) -> tuple[int, ...]: ...

    def page_facts(self, hwnd: int) -> PageFacts: ...


def in_place_url(
    document: str | None, bar: str | None, *, bar_read: bool
) -> tuple[str | None, bool]:
    """([V2.7], [V2.8]) the in-place URL and whether it is observed: the Document's scheme and
    host with the bar's path, query and fragment, accepted only on the Document's host (or the
    bar's elided `www.`). No bar at all is not observed; a failed read, no Document to check
    against, or another host is unread."""
    if not bar_read:
        return None, True
    if bar is None:
        return None, False
    if document is None:
        return None, True
    doc = urllib.parse.urlsplit(document)
    try:
        shown = urllib.parse.urlsplit(bar if "://" in bar else f"{doc.scheme}://{bar}")
        host = shown.hostname
    except ValueError:
        return None, True
    if host is None or doc.hostname is None or not _same_host(host, doc.hostname):
        return None, True
    # [V2.8]: scheme and host from the Document; path, query and fragment from the bar
    composed = (doc.scheme, doc.netloc, shown.path, shown.query, shown.fragment)
    return urllib.parse.urlunsplit(composed), True


def _same_host(bar_host: str, document_host: str) -> bool:
    """[V2.8] equal, or the bar shows the Document's host with one leading `www.` removed;
    never the reverse, and any other difference (a Unicode host against punycode) is not."""
    return bar_host == document_host or (
        document_host.startswith("www.") and bar_host == document_host[len("www.") :]
    )


def read_observed(  # noqa: PLR0913 - the snapshot, the reader and each optional fact, keyword-only
    snapshot: Snapshot,
    reader: FactReader,
    *,
    key: bytes,
    windows: Callable[[], tuple[int, ...]] | None = None,
    acted_value: Callable[[], str | None] | None = None,
    page_facts: bool = True,
    acted: int | None = None,
) -> Observed:
    """`snapshot` with the facts read now. `page_facts=False` skips the URLs, roles and headings
    (a window without a Document); `acted_value` is None for no acted field or a secure one.
    [V2.26] (2): `acted` is the acted field's id; its value is never read when its secure flag
    is set or was not read (no digest from a field that may be a password)."""
    document_url = url = None
    facts: PageFacts | None = None
    url_observed = True
    if page_facts:
        document_url, url, url_observed = _urls(snapshot, reader)
        try:
            facts = reader.page_facts(snapshot.hwnd)
        except OSError:
            facts = None
    try:
        focus: tuple[int, ...] | None = reader.focused_runtime_id() or None
    except OSError:
        focus = None
    try:
        listed = None if windows is None else windows()
    except OSError:
        listed = None
    value = None
    if acted_value is not None and not _maybe_secure(snapshot, acted):
        try:
            text = acted_value()
        except OSError:
            text = None
        value = None if text is None else value_fact(text, key=key)
    return Observed(
        snapshot=snapshot,
        url=url,
        windows=listed,
        focus=focus,
        acted_value=value,
        headings=None if facts is None else facts.headings,
        aria_roles=None if facts is None else facts.roles,
        landmarks=None if facts is None else facts.landmarks,
        document_url=document_url,
        url_observed=url_observed,
    )


def _maybe_secure(snapshot: Snapshot, acted: int | None) -> bool:
    if acted is None:
        return False
    if acted in snapshot.unread_secure:
        return True
    return any(e.id == acted and e.secure for e in snapshot.elements)


def _urls(snapshot: Snapshot, reader: FactReader) -> tuple[str | None, str | None, bool]:
    top = top_document(snapshot)
    try:
        found = None if top is None else reader.document_url_in(snapshot, top.id)
        document = None if found is None else found.url
    except OSError:
        document = None
    try:
        bar, bar_read = reader.address_bar(snapshot.hwnd), True
    except OSError:
        bar, bar_read = None, False
    url, observed = in_place_url(document, bar, bar_read=bar_read)
    return document, url, observed
