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
- `headings`, `aria_roles`: one cached fetch of RuntimeId, AriaRole and HeadingLevel.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from typing import Protocol

from steerai.world.address_bar import DocumentUrl
from steerai.world.effects import Observed, top_document, value_fact
from steerai.world.model import Snapshot


class FactReader(Protocol):
    def document_url_in(self, snapshot: Snapshot, document_id: int) -> DocumentUrl | None: ...

    def address_bar(self, hwnd: int) -> str | None: ...

    def focused_runtime_id(self) -> tuple[int, ...]: ...

    def roles_and_headings(
        self, hwnd: int
    ) -> tuple[dict[tuple[int, ...], str], dict[tuple[int, ...], int]]: ...


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
) -> Observed:
    """`snapshot` with the facts read now. `page_facts=False` skips the URLs, roles and headings
    (a window without a Document); `acted_value` is None for no acted field or a secure one."""
    document_url = url = roles = headings = None
    url_observed = True
    if page_facts:
        document_url, url, url_observed = _urls(snapshot, reader)
        try:
            roles, headings = reader.roles_and_headings(snapshot.hwnd)
        except OSError:
            roles = headings = None
    try:
        focus: tuple[int, ...] | None = reader.focused_runtime_id() or None
    except OSError:
        focus = None
    try:
        listed = None if windows is None else windows()
    except OSError:
        listed = None
    value = None
    if acted_value is not None:
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
        headings=headings,
        aria_roles=roles,
        document_url=document_url,
        url_observed=url_observed,
    )


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
