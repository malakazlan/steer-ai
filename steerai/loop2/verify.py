"""The code verifier's evidence rule for step 3 ([V2.3], [V2.11] decision 7): cited evidence must
be inside the scope Document; a tab name, the address bar or another Document never is."""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from steerai.loop2.decision import Claim
from steerai.loop2.owned import field_owned
from steerai.loop2.task import Subgoal
from steerai.world.effects import Observed, ValueFact, observe, top_document
from steerai.world.elements import Element
from steerai.world.model import Snapshot


def _inside(element: Element, by_id: dict[int, Element], scope: tuple[int, ...]) -> bool:
    """`element` is the scope Document or has it among its ancestors."""
    seen: set[int] = set()
    current: Element | None = element
    while current is not None and current.id not in seen:
        if current.runtime_id == scope:
            return True
        seen.add(current.id)
        current = None if current.parent_id is None else by_id.get(current.parent_id)
    return False


def evidence_tokens(
    claim: Claim, snapshot: Snapshot, *, scope_document: tuple[int, ...] | None
) -> tuple[str, ...]:
    """Reject tokens for the claim's cited ids in `snapshot`: `id_not_listed` when an id is not
    in the snapshot; `outside_scope` when an id is not inside the scope Document (no Document
    ancestor, or another Document: a side panel, a split view). With no scope Document the
    whole window is allowed. Empty when every cited element is admissible."""
    by_id = {e.id: e for e in snapshot.elements}
    tokens: list[str] = []
    for cited in claim.evidence:
        element = by_id.get(cited)
        if element is None:
            tokens.append("id_not_listed")
        elif scope_document is not None and not _inside(element, by_id, scope_document):
            tokens.append("outside_scope")
    return tuple(dict.fromkeys(tokens))


@dataclass(frozen=True)
class Readers:
    """[V2.29] Q2: the live reads a code claim may make, each three-valued (None: unread). The
    loop binds them to its ports; the claim checks never read anything else."""

    read_state: Callable[[Element, str], bool | None]
    """Whether the element is in the wanted state (on, off, selected, expanded, collapsed)."""
    read_value: Callable[[Element], ValueFact | None]
    """The element's value fact (filled and the keyed digest), never its text."""
    fact_of: Callable[[str], ValueFact]
    """The value fact a field holding exactly this text would read as (same key, same
    normalisation), so a value claim compares digests."""


def check_claim(  # noqa: PLR0911, PLR0913 - one return per outcome; keyword-only readers
    claim: Claim,
    subgoal: Subgoal,
    baseline: Observed,
    now: Observed,
    *,
    readers: Readers,
    scope_document: tuple[int, ...] | None = None,
    typed: Sequence[tuple[int, ...]] = (),
) -> tuple[str, ...]:
    """A `state`, `value` or `navigated` claim checked by code (§7.1): the reject tokens, empty
    when it is proven. `state` and `value` cite exactly one element in the scope Document (the
    top Document of `now` unless given) and read it live ([V2.29] Q2): an unread read rejects
    with its own token, never as "false"; a field whose secure flag is set or unread is never
    read. `navigated` is measured since the subgoal's `baseline`."""
    if claim.kind == "navigated":
        return navigated_tokens(subgoal, baseline, now, cited=claim.evidence, typed=typed)
    if not claim.evidence:
        return ("evidence_missing",)
    if len(claim.evidence) > 1:
        return ("too_much_evidence",)
    if scope_document is None:
        top = top_document(now.snapshot)
        scope_document = None if top is None else top.runtime_id
    rejected = evidence_tokens(claim, now.snapshot, scope_document=scope_document)
    if rejected:
        return rejected
    element = next(e for e in now.snapshot.elements if e.id == claim.evidence[0])
    if claim.kind == "state":
        if claim.wanted is None:  # validate refuses this shape first
            raise ValueError("wanted_missing")
        held = readers.read_state(element, claim.wanted)
        if held is None:
            return ("state_unread",)
        return () if held else ("state_mismatch",)
    if element.secure:
        return ("secure_field",)
    if element.id in now.snapshot.unread_secure:
        return ("secure_unread",)
    if claim.param is None or claim.param not in subgoal.params:  # validate refuses these first
        raise ValueError("param_missing")
    read = readers.read_value(element)
    if read is None:
        return ("value_unread",)
    return () if read == readers.fact_of(subgoal.params[claim.param].text) else ("value_mismatch",)


NAVIGATED_SCHEMES: Final = frozenset({"http", "https", "file"})
"""§7.1 ([V2.3]): `navigated` is an allow-list; `file` for the local fixtures. An error page
(`chrome-error:`) or an unread URL is never navigated."""
URL_CARRIERS: Final = frozenset({"url_param", "url_path"})
CHANGE: Final = frozenset({"navigated", "url_changed_in_place"})
OWNING_ROLES: Final = frozenset({"Menu", "ToolTip"})
OPTION_CAPS: Final = frozenset({"selectable", "selected"})
ProofToken = Literal["title", "heading", "results_with_url"]
"""How the auto proof closed a subgoal; `results_with_url` is counted on the pooled run
([V2.29] Q1): one false done on it reverts the rule."""


@dataclass(frozen=True)
class _Found:
    page: ProofToken | None
    """A title or heading carrier: a page-content signal by itself."""
    url: bool
    results: bool


def navigated_tokens(
    subgoal: Subgoal,
    baseline: Observed,
    now: Observed,
    *,
    cited: Sequence[int],
    typed: Sequence[tuple[int, ...]] = (),
) -> tuple[str, ...]:
    """§7.1 `navigated` since the subgoal's baseline, for a claim citing `cited`: the reject
    tokens, empty when proven. The content signal is a title or heading carrier, or the cited
    elements, each new since the baseline, inside the scope Document and owned by no typed
    field."""
    found = _navigated(subgoal, baseline, now, typed=typed)
    if isinstance(found, tuple):
        return found
    if found.page is not None:
        return ()
    if not cited:
        return ("no_content",)
    rejected: list[str] = []
    for eid in cited:
        rejected.extend(_cited_tokens(eid, baseline, now, typed))
    return tuple(dict.fromkeys(rejected))


def auto_proof(
    subgoal: Subgoal, baseline: Observed, now: Observed, *, typed: Sequence[tuple[int, ...]] = ()
) -> ProofToken | None:
    """§7.1, [V2.29] Q1: the token of the code-only `navigated` proof of `subgoal` since its
    baseline, or None. The content signal is a title or heading carrier, or the `results`
    carrier together with a URL carrier: an element holding the words that is new since the
    baseline, inside the scope Document, not under a Menu or ToolTip, owned by no typed field,
    and not a selectable option when Steer typed the words."""
    found = _navigated(subgoal, baseline, now, typed=typed)
    if isinstance(found, tuple):
        return None
    if found.page is not None:
        return found.page
    # past a page carrier, `_navigated` only returns a change that has a URL carrier
    return "results_with_url" if found.results else None


def _navigated(  # noqa: PLR0911 - one return per closed outcome
    subgoal: Subgoal, baseline: Observed, now: Observed, *, typed: Sequence[tuple[int, ...]]
) -> _Found | tuple[str, ...]:
    """The carriers since the baseline, or the reject tokens when it is no navigation at all."""
    if baseline.tier != now.tier:
        return ("tier_mismatch",)
    address = now.document_url
    if address is None:
        return ("url_unread",)
    if urllib.parse.urlsplit(address).scheme not in NAVIGATED_SCHEMES:
        return ("not_a_page",)
    texts = [span.text for span in subgoal.params.values()]
    if not texts:
        return ("param_missing",)
    acted = typed[0] if typed else None
    best: _Found | None = None
    for text in texts:
        report = observe(baseline, now, acted=acted, text=text)
        if not report.classes & CHANGE:
            continue
        carriers = report.carriers
        page: ProofToken | None = None
        if "title" in carriers:
            page = "title"
        elif "heading" in carriers:
            page = "heading"
        found = _Found(
            page=page,
            url=bool(carriers & URL_CARRIERS),
            results=_results(text, baseline, now, typed),
        )
        if found.page is not None or (found.url and found.results):
            return found
        best = best or found
    if best is None:
        return ("no_change",)
    if not best.url:
        return ("no_carrier",)
    return best


def _words(text: str) -> list[str]:
    """Casefolded whole tokens of the NFKC form, as the observer's carriers read them."""
    return re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())


def _holds(name: str, words: Sequence[str]) -> bool:
    have = _words(name)
    n = len(words)
    return n > 0 and any(have[i : i + n] == list(words) for i in range(len(have) - n + 1))


def _results(
    text: str, baseline: Observed, now: Observed, typed: Sequence[tuple[int, ...]]
) -> bool:
    """[V2.29] Q1: an element holding the words that passes the exclusions of a cited claim."""
    words = _words(text)
    before = {e.runtime_id: e.name for e in baseline.snapshot.elements if e.runtime_id}
    by_id = {e.id: e for e in now.snapshot.elements}
    top = top_document(now.snapshot)
    for element in now.snapshot.elements:
        if not _holds(element.name, words) or element.runtime_id in typed:
            continue
        if element.runtime_id and before.get(element.runtime_id) == element.name:
            continue  # present before the baseline
        if top is not None and not _inside(element, by_id, top.runtime_id):
            continue
        if _under_role(element, by_id, OWNING_ROLES):
            continue
        if typed and element.role == "ListItem" and OPTION_CAPS & set(element.caps):
            continue  # the option rule: an option echoing what Steer typed
        if any(field_owned(element, now, field) for field in typed):
            continue
        return True
    return False


def _cited_tokens(
    eid: int, baseline: Observed, now: Observed, typed: Sequence[tuple[int, ...]]
) -> list[str]:
    by_id = {e.id: e for e in now.snapshot.elements}
    element = by_id.get(eid)
    if element is None:
        return ["id_not_listed"]
    top = top_document(now.snapshot)
    if top is not None and not _inside(element, by_id, top.runtime_id):
        return ["outside_scope"]
    before = {e.runtime_id: e.name for e in baseline.snapshot.elements if e.runtime_id}
    if element.runtime_id and before.get(element.runtime_id) == element.name:
        return ["not_in_delta"]
    owned: list[str] = []
    for field in typed:
        owned.extend(field_owned(element, now, field))
    return owned


def _under_role(element: Element, by_id: dict[int, Element], roles: frozenset[str]) -> bool:
    seen: set[int] = set()
    parent = element.parent_id
    while parent is not None and parent not in seen:
        seen.add(parent)
        current = by_id.get(parent)
        if current is None:
            return False
        if current.role in roles:
            return True
        parent = current.parent_id
    return False
