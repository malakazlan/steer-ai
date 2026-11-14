"""[V2.28] (b): why a site sample's in-place URL is unread.

`observe2.in_place_url` returns no URL for four reasons the sample row could not tell apart:
the window has no address bar (`url_observed` false), the bar read failed, the top Document's
URL was not read, or the bar's host is not the Document's (a `www.` the bar kept, a Unicode host
against punycode). `bar_shape` and `bar_www_elided` were taken at the end of the sample, after
the late read, so they described the settled page, not the one the watch ended on. These fields
are taken from the watch's own final read and a bar read made right after it. Closed tokens,
booleans and a count: never a URL, a host or a title.
"""

from __future__ import annotations

from typing import Any, Protocol

from steerai.world.effects import Observed, top_document
from steerai.world.observe2 import in_place_url


class BarReader(Protocol):
    def address_bar(self, hwnd: int) -> str | None: ...


def read_bar(reader: BarReader, hwnd: int) -> tuple[str | None, bool]:
    """The address bar now, and whether the read failed (an OSError is said, never raised)."""
    try:
        return reader.address_bar(hwnd), False
    except OSError:
        return None, True


def url_cause(after: Observed, bar: str | None, *, bar_error: bool) -> str:
    """The one reason `after`'s URL is what it is: `read`; `no_bar` (not observed);
    `document_url_unread`; `bar_error` (the re-read failed); `readable_now` (the watch's read had
    no URL but the bar read right after it composes one: the page moved on); or `host_mismatch`
    (a Document URL and a bar that do not agree on the host, or a bar that does not parse).
    `after` is the watch's final read."""
    if after.url is not None:
        return "read"
    if not after.url_observed:
        return "no_bar"
    if after.document_url is None:
        return "document_url_unread"
    if bar_error:
        return "bar_error"
    composed, _ = in_place_url(after.document_url, bar, bar_read=True)
    return "readable_now" if composed is not None else "host_mismatch"


def url_fields(after: Observed, bar: str | None, *, bar_error: bool) -> dict[str, Any]:  # Any: JSON
    """The row fields for one site sample, from the watch's final read."""
    top = top_document(after.snapshot)
    children = (
        0 if top is None else sum(1 for e in after.snapshot.elements if e.parent_id == top.id)
    )
    return {
        "url_cause": url_cause(after, bar, bar_error=bar_error),
        "document_url_read": after.document_url is not None,
        "after_stable": after.snapshot.stable,
        "document_children": children,
        "top_document": top is not None,
    }
