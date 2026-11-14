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
from dataclasses import dataclass
from typing import Protocol

from steerai.world.address_bar import DocumentUrl
from steerai.world.effects import Observed, top_document, value_fact
from steerai.world.landmarks import PageFacts
from steerai.world.model import Snapshot

FIRST_READY_S = 10.0
"""[V2.27] how long the first observation of a process's window waits for its tree to be built;
a guard, unmeasured. Past it the last read carries the unstable token."""
FIRST_POLL_S = 0.25
FIRST_QUIET_S = 3.0
"""[V2.28] 2: how long the node count must hold, after the first touch, before a first
observation is ready; provisional. Measured (first_observation --measure, 2026-11-14, Chrome 157,
a read every 0.25 s to 10 s): on gh1 a partial Document (156 of 303 nodes) held 0.75-1.0 s,
1.25-1.5 s and 2.0-2.5 s in three loads before it grew; a window with no Document yet held its
node count up to 1.25-1.75 s (gh1, mdn1). 3 s is above each of those. Not covered: a page still
loading over the network. Read every 0.25 s to 15 s beside DevTools' readyState, gh1 held a
partial tree for up to 12.3 s (readyState `loading` or `interactive`); no quiet window under
`FIRST_READY_S` tells that from a built page."""


def document_nodes(snapshot: Snapshot) -> frozenset[int] | None:
    """[V2.28] 2: the ids of every element under the top Document (the Document itself not
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


def _size(snapshot: Snapshot) -> tuple[bool, int]:
    """What must hold across the quiet window: the scope Document's node count, or the whole
    window's when it has no Document (a native window, or Chrome before its Document appears)."""
    inside = document_nodes(snapshot)
    return (False, snapshot.node_count) if inside is None else (True, len(inside))


@dataclass(slots=True)
class _Touch:
    size: tuple[bool, int]
    since: float
    ready: bool = False


class FirstTouches:
    """[V2.28] 2: first-observation readiness, one per task. Keyed on the first touch of a
    process (its pid; the window handle when the pid was not read), never the browser's name:
    Chrome builds its tree lazily from the first UIA touch.

    A process's window is ready when the snapshot is stable, its scope Document has children,
    and the Document's node count has not changed for `quiet_s`; the quiet window starts at the
    first touch, so one read is never enough. A window with no Document is judged on its whole
    node count. Once ready, the process stays ready for the task."""

    def __init__(
        self, *, clock: Callable[[], float] = time.monotonic, quiet_s: float = FIRST_QUIET_S
    ) -> None:
        if quiet_s <= 0:
            raise ValueError("the quiet window must be positive")
        self._clock = clock
        self._quiet_s = quiet_s
        self._touches: dict[tuple[str, int], _Touch] = {}

    def ready(self, snapshot: Snapshot) -> bool:
        """Whether this read of `snapshot`'s process is ready; the first call is its first touch."""
        now = self._clock()
        key = ("pid", snapshot.pid) if snapshot.pid else ("hwnd", snapshot.hwnd)
        size = _size(snapshot)
        touch = self._touches.setdefault(key, _Touch(size, now))
        if touch.ready:
            return True
        if size != touch.size:
            touch.size, touch.since = size, now
        has_document, count = size
        built = count > 0 or not has_document
        touch.ready = snapshot.stable and built and now - touch.since >= self._quiet_s
        return touch.ready


def first_observation(  # noqa: PLR0913 - the read, the task's touches and the injected clock
    read: Callable[[], Observed],
    *,
    touches: FirstTouches | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    deadline_s: float = FIRST_READY_S,
    poll_s: float = FIRST_POLL_S,
) -> Observed:
    """A turn's observation of a window: `read` (a full v2 read) until `touches` says its
    process is ready ([V2.28] 2), within `deadline_s`. A process already ready in this task
    returns its first read. At the deadline the last read is returned with the unstable token,
    never handed over as a stable, partial listing. Without `touches` this call is the
    process's first touch."""
    touches = FirstTouches(clock=clock) if touches is None else touches
    deadline = clock() + deadline_s
    while True:
        seen = read()
        if touches.ready(seen.snapshot):
            return seen
        if clock() >= deadline:
            unstable = dataclasses.replace(seen.snapshot, stable=False)
            return dataclasses.replace(seen, snapshot=unstable)
        sleep(poll_s)


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
