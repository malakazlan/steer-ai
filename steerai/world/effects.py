"""The effect observer (V2 §6.1 `Delta`, §6.4 `EffectReport`, as amended by [V2.2]; step 2a).

Pure: two observations and what was acted on give a typed report of what changed. No reads, no
model, no decision. A report is never a proof (step 3's verifier owns the tiers, V2 §7.1).

The owner's four conditions ([V2.2], 2026-11-13):
1. `none` means proven nothing changed. A page re-render (`rerendered`) or a removal such as a
   dropdown closing (`dismissed`) is a token, never `none`. A box move alone (`moved`) may be.
2. `unknown`, and any fact in `unread`, is never `none`.
3. `classes` is the full set; `primary` is one class, for logs only.
4. A changed window list is the token `windows_changed`, never `none`.

The rules reuse v1's: "outside the field" is `loop._new_outside`'s exemption (NE-01/01b), the
top Document is `loop._top_document`'s (NE-13), and `url_param` is `url_query.carries`.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from steerai.verify.url_query import carries
from steerai.world.elements import Element
from steerai.world.model import TRANSIENT_ROLES, Snapshot, normalise_name

EffectClass = Literal[
    "none",
    "own_state",
    "revealed",
    "content_changed",
    "navigated",
    "url_changed_in_place",
    "unknown",
]
Token = Literal["rerendered", "dismissed", "moved", "title_only", "windows_changed"]
Unread = Literal["url", "windows", "caps"]
Carrier = Literal["url_param", "url_path", "title", "results"]

PRECEDENCE: Final[tuple[EffectClass, ...]] = (
    "unknown",
    "navigated",
    "url_changed_in_place",
    "revealed",
    "own_state",
    "content_changed",
    "none",
)
"""The order `primary` is chosen in, for logs only (condition 3)."""
PAGE_ROLES: Final = frozenset({"Document", "Pane", "Window"})
"""As `loop._PAGE_ROLES`: a field whose parent is the page is exempt only for its own subtree."""
RERENDER_SIMILARITY: Final = 0.9
"""A replaced top Document whose element names are this similar (multiset) is a re-render."""
MAX_CHAIN: Final = 64
"""Ancestors kept in a re-render key: deeper chains are cut, never followed further."""
MIN_TOKEN_CHARS: Final = 3
"""A single-token text shorter than this gives no title or results carrier (hunter EO-21)."""


@dataclass(frozen=True, slots=True)
class Observed:
    """One observation and the facts read with it: None is not read, never assumed."""

    snapshot: Snapshot
    url: str | None = None
    windows: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class EffectReport:
    classes: frozenset[EffectClass]
    """The full set (condition 3). Empty only when tokens or unread facts bar `none`."""
    tokens: frozenset[Token]
    unread: frozenset[Unread]
    primary: EffectClass | None
    """One class by `PRECEDENCE`, for logs only; None when `classes` is empty."""
    revealed_root: int | None
    carriers: frozenset[Carrier]
    stable_after: bool


def observe(
    before: Observed,
    after: Observed,
    *,
    acted: tuple[int, ...] | None = None,
    text: str | None = None,
) -> EffectReport:
    """What changed from `before` to `after`, the element `acted` on given by RuntimeId."""
    b, a = before.snapshot, after.snapshot
    unread = _unread(before, after)
    if b.hwnd != a.hwnd or not a.stable or a.minimized:
        return _report({"unknown"}, set(), unread, None, set(), a.stable)
    top_b, top_a = _top_document(b), _top_document(a)
    if (top_b is None) != (top_a is None):
        return _report({"unknown"}, set(), unread, None, set(), a.stable)
    tokens: set[Token] = set()
    windows_read = before.windows is not None and after.windows is not None
    if windows_read and set(before.windows or ()) != set(after.windows or ()):
        tokens.add("windows_changed")
    carriers = _carriers(before, after, acted, text)
    classes: set[EffectClass] = set()
    if top_b is not None and top_a is not None and top_b.runtime_id != top_a.runtime_id:
        if _rerendered_page(before, after, top_b, top_a):
            tokens.add("rerendered")
        else:
            return _report({"navigated"}, tokens, unread, None, carriers, a.stable)
    if before.url is not None and after.url is not None and before.url != after.url:
        classes.add("url_changed_in_place")
    changes = _changes(b, a, acted, caps_read="caps" not in unread)
    classes |= changes.classes
    tokens |= changes.tokens
    if b.title != a.title and not classes and not changes.tokens - {"moved"}:
        tokens.add("title_only")
    if not classes and not (tokens - {"moved"}) and not unread:
        classes.add("none")
    return _report(classes, tokens, unread, changes.revealed_root, carriers, a.stable)


# ---- element changes ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Changes:
    classes: frozenset[EffectClass]
    tokens: frozenset[Token]
    revealed_root: int | None


def _changes(
    before: Snapshot, after: Snapshot, acted: tuple[int, ...] | None, *, caps_read: bool
) -> _Changes:
    b_elems = [e for e in before.elements if e.role not in TRANSIENT_ROLES]
    a_elems = [e for e in after.elements if e.role not in TRANSIENT_ROLES]
    b_by = {e.runtime_id: e for e in b_elems if e.runtime_id}
    a_by = {e.runtime_id: e for e in a_elems if e.runtime_id}
    same = {rid for rid in b_by.keys() & a_by.keys() if b_by[rid].role == a_by[rid].role}
    appeared = [e for e in a_elems if e.runtime_id and e.runtime_id not in same]
    gone = [e for e in b_elems if e.runtime_id and e.runtime_id not in same]
    appeared, gone, rerendered = _pair_rerenders(before, after, appeared, gone)
    anonymous = _anonymous(b_elems) != _anonymous(a_elems)
    classes: set[EffectClass] = set()
    tokens: set[Token] = set()
    if rerendered:
        tokens.add("rerendered")
    acted_after = a_by.get(acted) if acted else None
    acted_before = b_by.get(acted) if acted else None
    exempt = _exemption(after, acted_after)
    under_acted = set() if acted_after is None else _under(after, acted_after.id)
    revealed = [e for e in appeared if e.id in under_acted and e.id != acted_after.id]  # type: ignore[union-attr]  # under_acted is empty when acted_after is None
    gained_expanded = (
        caps_read
        and acted_before is not None
        and acted_after is not None
        and "expanded" in acted_after.caps
        and "expanded" not in acted_before.caps
    )
    root = _outermost(revealed)
    if revealed or gained_expanded:
        classes.add("revealed")
    revealed_ids = set() if root is None else _under(after, root)
    for rid in same:
        old, new = b_by[rid], a_by[rid]
        fields = _changed_fields(old, new, caps_read=caps_read)
        if not fields:
            continue
        if rid == acted:
            if fields == {"caps"} and normalise_name(old.name) == normalise_name(new.name):
                classes.add("own_state")
            elif fields != {"box"}:
                classes.add("content_changed")
            continue
        if fields == {"box"}:
            tokens.add("moved")
        elif new.id not in exempt and new.id not in revealed_ids:
            classes.add("content_changed")
    revealed_set = {e.id for e in revealed}
    outside = [
        e
        for e in appeared
        if e.id not in exempt and e.id not in revealed_ids and e.id not in revealed_set
    ]
    if outside or anonymous:
        classes.add("content_changed")
    if gone:
        tokens.add("dismissed")
    return _Changes(frozenset(classes), frozenset(tokens), root)


def _changed_fields(old: Element, new: Element, *, caps_read: bool) -> set[str]:
    fields = set()
    if old.name != new.name:
        fields.add("name")
    if old.enabled != new.enabled:
        fields.add("enabled")
    if old.box != new.box:
        fields.add("box")
    if old.offscreen != new.offscreen:
        fields.add("offscreen")
    if caps_read and set(old.caps) != set(new.caps):
        fields.add("caps")
    return fields


def _chains(snapshot: Snapshot) -> Callable[[Element], tuple[tuple[str, str], ...]]:
    """Each element's ancestors as (role, normalised name), nearest first, at most
    `MAX_CHAIN`, memoised per snapshot and iterative: linear in nodes (hunter EO-29), never
    deep recursion, and a parent cycle in a hostile tree ends at the first repeat."""
    by_id = {e.id: e for e in snapshot.elements}
    memo: dict[int, tuple[tuple[str, str], ...]] = {}

    def chain_of(element: Element) -> tuple[tuple[str, str], ...]:
        out: list[tuple[str, str]] = []
        seen: set[int] = set()
        parent_id = element.parent_id
        while parent_id is not None and parent_id in by_id and len(out) < MAX_CHAIN:
            if parent_id in memo:
                out.extend(memo[parent_id])
                break
            if parent_id in seen:
                break
            seen.add(parent_id)
            parent = by_id[parent_id]
            out.append((parent.role, normalise_name(parent.name)))
            parent_id = parent.parent_id
        chain = tuple(out[:MAX_CHAIN])
        if element.id in by_id:
            memo[element.id] = ((element.role, normalise_name(element.name)), *chain)[:MAX_CHAIN]
        return chain

    return chain_of


def _pair_rerenders(
    before: Snapshot, after: Snapshot, appeared: list[Element], gone: list[Element]
) -> tuple[list[Element], list[Element], bool]:
    """Drop appeared/gone pairs with the same role, normalised name and ancestor chain: a
    re-render, never an appearance ([V2.2] §6.1)."""
    chain_before, chain_after = _chains(before), _chains(after)
    pool: dict[tuple[object, ...], list[Element]] = {}
    for element in gone:
        key = (element.role, normalise_name(element.name), chain_before(element))
        pool.setdefault(key, []).append(element)
    kept: list[Element] = []
    paired = False
    for element in appeared:
        key = (element.role, normalise_name(element.name), chain_after(element))
        if pool.get(key):
            pool[key].pop()
            paired = True
        else:
            kept.append(element)
    left = [e for group in pool.values() for e in group]
    return kept, left, paired


def _anonymous(elements: Sequence[Element]) -> Counter[tuple[str, str]]:
    """Elements without a RuntimeId, as a multiset: a change among them is a change."""
    return Counter((e.role, normalise_name(e.name)) for e in elements if not e.runtime_id)


def _exemption(after: Snapshot, acted: Element | None) -> set[int]:
    """`loop._new_outside`'s exemption: the acted field's nearest container (its parent and
    everything under it), or its own subtree when that parent is the page or missing."""
    if acted is None:
        return set()
    by_id = {e.id: e for e in after.elements}
    parent = by_id.get(acted.parent_id) if acted.parent_id is not None else None
    root = acted.id if parent is None or parent.role in PAGE_ROLES else parent.id
    return _under(after, root)


def _under(snapshot: Snapshot, root_id: int) -> set[int]:
    children: dict[int | None, list[int]] = {}
    for element in snapshot.elements:
        children.setdefault(element.parent_id, []).append(element.id)
    found: set[int] = set()
    pending = [root_id]
    while pending:
        current = pending.pop()
        if current not in found:
            found.add(current)
            pending.extend(children.get(current, []))
    return found


def _outermost(elements: Sequence[Element]) -> int | None:
    """The one appeared element no other appeared element contains; None for several."""
    ids = {e.id for e in elements}
    roots = [e.id for e in elements if e.parent_id not in ids]
    return roots[0] if len(roots) == 1 else None


def _top_document(snapshot: Snapshot) -> Element | None:
    """As `loop._top_document`: the one Document with no Document above it; none or several is
    None."""
    by_id = {e.id: e for e in snapshot.elements}
    tops = []
    for element in snapshot.elements:
        if element.role != "Document":
            continue
        parent = by_id.get(element.parent_id) if element.parent_id is not None else None
        while parent is not None and parent.role != "Document":
            parent = by_id.get(parent.parent_id) if parent.parent_id is not None else None
        if parent is None:
            tops.append(element)
    return tops[0] if len(tops) == 1 else None


def _rerendered_page(before: Observed, after: Observed, top_b: Element, top_a: Element) -> bool:
    """A replaced top Document that is the same page: same name, an unchanged read URL and a
    name multiset at least `RERENDER_SIMILARITY` equal ([V2.2] §6.4, hunter EO-10)."""
    if normalise_name(top_b.name) != normalise_name(top_a.name):
        return False
    if before.url is None or after.url is None or before.url != after.url:
        return False
    old = Counter(normalise_name(e.name) for e in before.snapshot.elements)
    new = Counter(normalise_name(e.name) for e in after.snapshot.elements)
    total = max(sum(old.values()), sum(new.values()), 1)
    return sum((old & new).values()) / total >= RERENDER_SIMILARITY


# ---- facts not read and carriers ---------------------------------------------------------------


def _unread(before: Observed, after: Observed) -> frozenset[Unread]:
    out: set[Unread] = set()
    if before.url is None or after.url is None:
        out.add("url")
    if before.windows is None or after.windows is None:
        out.add("windows")
    if not (before.snapshot.caps_read and after.snapshot.caps_read):
        out.add("caps")
    return frozenset(out)


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())


def _contains(haystack: str, needle: Sequence[str]) -> bool:
    words = _tokens(haystack)
    n = len(needle)
    return n > 0 and any(words[i : i + n] == list(needle) for i in range(len(words) - n + 1))


def _segments(url: str) -> list[str]:
    path = urllib.parse.urlsplit(url).path
    return [
        " ".join(unicodedata.normalize("NFKC", urllib.parse.unquote(s)).split()).casefold()
        for s in path.split("/")
        if s
    ]


def _carriers(
    before: Observed, after: Observed, acted: tuple[int, ...] | None, text: str | None
) -> set[Carrier]:
    """Where the action's text now shows that it did not before ([V2.2] §6.4)."""
    if not text:
        return set()
    needle = _tokens(text)
    if not needle:
        return set()
    out: set[Carrier] = set()
    if before.url is not None and after.url is not None:
        if carries(after.url, text) and not carries(before.url, text):
            out.add("url_param")
        folded = " ".join(unicodedata.normalize("NFKC", text).split()).casefold()
        if folded in _segments(after.url) and folded not in _segments(before.url):
            out.add("url_path")
    if len(needle) == 1 and len(needle[0]) < MIN_TOKEN_CHARS:
        return out
    b, a = before.snapshot, after.snapshot
    if _contains(a.title, needle) and not _contains(b.title, needle):
        out.add("title")
    acted_after = next((e for e in a.elements if acted and e.runtime_id == acted), None)
    exempt = _exemption(a, acted_after)
    under = set() if acted_after is None else _under(a, acted_after.id)
    known = {e.runtime_id: e.name for e in b.elements if e.runtime_id}
    shown_before = any(
        _contains(e.name, needle) for e in b.elements if not (acted and e.runtime_id == acted)
    )
    if not shown_before:
        for element in a.elements:
            if element.id in exempt or element.id in under:
                continue
            new_name = known.get(element.runtime_id) != element.name
            if new_name and _contains(element.name, needle):
                out.add("results")
                break
    return out


def _report(  # noqa: PLR0913, PLR0917 - the report's fields, one each
    classes: set[EffectClass],
    tokens: set[Token],
    unread: frozenset[Unread],
    revealed_root: int | None,
    carriers: set[Carrier],
    stable_after: bool,
) -> EffectReport:
    primary = next((c for c in PRECEDENCE if c in classes), None)
    return EffectReport(
        classes=frozenset(classes),
        tokens=frozenset(tokens),
        unread=unread,
        primary=primary,
        revealed_root=revealed_root,
        carriers=frozenset(carriers),
        stable_after=stable_after,
    )
