"""The v2 observe path (V2 step 3b, [V2.5]): one `Observed` from the snapshot v1 already fetches
plus separate reads for the facts the observer needs. v1's fetch is unchanged; each extra read
is its own call, timed by the caller, and a read that fails is `None` (not observed), never a
guess.

- `url`: the top Document's URL (`DocumentUrl`), read with every snapshot of a page ([V2.4]).
- `windows`: the top-level window list.
- `focus`: the RuntimeId with keyboard focus.
- `acted_value`: the acted field's value as a `ValueFact` (filled + keyed digest); the text is
  dropped here and never kept. A secure field is never read: the caller passes no reader.
- `headings`, `aria_roles`: one cached fetch of RuntimeId, AriaRole and HeadingLevel.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from steerai.world.address_bar import DocumentUrl
from steerai.world.effects import Observed, value_fact
from steerai.world.model import Snapshot


class FactReader(Protocol):
    def document_url(self, hwnd: int) -> DocumentUrl | None: ...

    def focused_runtime_id(self) -> tuple[int, ...]: ...

    def roles_and_headings(
        self, hwnd: int
    ) -> tuple[dict[tuple[int, ...], str], dict[tuple[int, ...], int]]: ...


def read_observed(  # noqa: PLR0913 - the snapshot, the reader and each optional fact, keyword-only
    snapshot: Snapshot,
    reader: FactReader,
    *,
    key: bytes,
    windows: Callable[[], tuple[int, ...]] | None = None,
    acted_value: Callable[[], str | None] | None = None,
    page_facts: bool = True,
) -> Observed:
    """`snapshot` with the facts read now. `page_facts=False` skips the URL, roles and headings
    (a window without a Document); `acted_value` is None for no acted field or a secure one."""
    url = roles = headings = None
    if page_facts:
        try:
            document = reader.document_url(snapshot.hwnd)
            url = None if document is None else document.url
        except OSError:
            url = None
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
    )
