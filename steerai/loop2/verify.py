"""The code verifier's evidence rule for step 3 ([V2.3], [V2.11] decision 7): cited evidence must
be inside the scope Document; a tab name, the address bar or another Document never is."""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from steerai.loop2.decision import Claim
from steerai.loop2.owned import field_owned
from steerai.loop2.task import Subgoal
from steerai.world.effects import Observed, ValueFact, observe, result_elements, top_document
from steerai.world.elements import Element
from steerai.world.model import Snapshot, normalise_name


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
    acted: tuple[int, ...] | None = None,
    echo: bool = False,
) -> tuple[str, ...]:
    """A `state`, `value` or `navigated` claim checked by code (§7.1): the reject tokens, empty
    when it is proven. `state` and `value` cite exactly one element in the scope Document (the
    top Document of `now` unless given) and read it live ([V2.29] Q2): an unread read rejects
    with its own token, never as "false"; a field whose secure flag is set or unread is never
    read. `navigated` is measured since the subgoal's `baseline`."""
    if claim.kind == "navigated":
        return navigated_tokens(
            subgoal, baseline, now, cited=claim.evidence, typed=typed, acted=acted, echo=echo
        )
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
"""§7.1 ([V2.3]): `navigated` is an allow-list; `file` for the local fixtures ([V2.39] narrows it
to a task whose start URL is `file:`, in the PR after this one). An error page (`chrome-error:`)
or an unread URL is never navigated."""
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


def navigated_tokens(  # noqa: PLR0913 - the subgoal, both reads and keyword-only facts
    subgoal: Subgoal,
    baseline: Observed,
    now: Observed,
    *,
    cited: Sequence[int],
    typed: Sequence[tuple[int, ...]] = (),
    acted: tuple[int, ...] | None = None,
    echo: bool = False,
) -> tuple[str, ...]:
    """§7.1 `navigated` since the subgoal's baseline, for a claim citing `cited`: the reject
    tokens, empty when proven. The content signal is a title or heading carrier, or the cited
    elements, each absent before the baseline, inside the scope Document, and passing the same
    exclusions as the auto proof's results (`_excluded`). `typed` is every field Steer typed in
    the task, `echo` whether the subgoal's words are text Steer typed ([V2.38] RT-39), `acted`
    the field typed under this subgoal (its Document scopes the carriers)."""
    found = _navigated(subgoal, baseline, now, typed=typed, acted=acted, echo=echo)
    if isinstance(found, tuple):
        return found
    if found.page is not None:
        return ()
    if not cited:
        return ("no_content",)
    rejected: list[str] = []
    for eid in cited:
        rejected.extend(_cited_tokens(eid, baseline, now, typed=typed, echo=echo))
    return tuple(dict.fromkeys(rejected))


def auto_proof(  # noqa: PLR0913 - the subgoal, both reads and keyword-only facts
    subgoal: Subgoal,
    baseline: Observed,
    now: Observed,
    *,
    typed: Sequence[tuple[int, ...]] = (),
    acted: tuple[int, ...] | None = None,
    echo: bool = False,
) -> ProofToken | None:
    """§7.1, [V2.29] Q1: the token of the code-only `navigated` proof of `subgoal` since its
    baseline, or None. The content signal is a title or heading carrier, or the `results`
    carrier together with a URL carrier: an element the observer counts as a result (§6.4: in
    the scope Document, the words absent before) that passes `_excluded`."""
    found = _navigated(subgoal, baseline, now, typed=typed, acted=acted, echo=echo)
    if isinstance(found, tuple):
        return None
    if found.page is not None:
        return found.page
    # past a page carrier, `_navigated` only returns a change that has a URL carrier
    return "results_with_url" if found.results else None


def _navigated(  # noqa: PLR0911, PLR0913 - one return per closed outcome; keyword-only facts
    subgoal: Subgoal,
    baseline: Observed,
    now: Observed,
    *,
    typed: Sequence[tuple[int, ...]],
    acted: tuple[int, ...] | None,
    echo: bool,
) -> _Found | tuple[str, ...]:
    """The carriers since the baseline, or the reject tokens when it is no navigation at all."""
    if baseline.tier != now.tier:
        return ("tier_mismatch",)
    address = now.document_url
    if address is None:
        return ("url_unread",)
    if urllib.parse.urlsplit(address).scheme.lower() not in NAVIGATED_SCHEMES:
        return ("not_a_page",)
    texts = [span.text for span in subgoal.params.values()]
    if not texts:
        return ("param_missing",)
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
        results = any(
            not _excluded(e, now, typed=typed, echo=echo)
            for e in result_elements(baseline, now, acted=acted, text=text)
        )
        found = _Found(page=page, url=bool(carriers & URL_CARRIERS), results=results)
        if found.page is not None or (found.url and found.results):
            return found
        best = best or found
    if best is None:
        return ("no_change",)
    if not best.url:
        return ("no_carrier",)
    return best


def _excluded(
    element: Element, now: Observed, *, typed: Sequence[tuple[int, ...]], echo: bool
) -> list[str]:
    """[V2.29] Q1: why an element proves nothing, for the auto proof and a cited claim alike:
    outside the single top Document (§7.1: a tab name or the address bar is never evidence), or
    no single top Document at all (fail closed: never the whole window); a typed field itself;
    under a Menu or ToolTip; a selectable option when the subgoal's words are text Steer typed
    (the option rule, task-wide); owned by any field Steer typed in the task."""
    by_id = {e.id: e for e in now.snapshot.elements}
    top = top_document(now.snapshot)
    if top is None:
        return ["no_scope"]
    if not _inside(element, by_id, top.runtime_id):
        return ["outside_scope"]
    if element.runtime_id in typed:
        return ["inside_field"]
    tokens: list[str] = []
    if _under_role(element, by_id, OWNING_ROLES):
        tokens.append("under_suggestions")
    if echo and element.role == "ListItem" and OPTION_CAPS & set(element.caps):
        tokens.append("option_evidence")
    for field in typed:
        tokens.extend(field_owned(element, now, field))
    return list(dict.fromkeys(tokens))


def _cited_tokens(
    eid: int, baseline: Observed, now: Observed, *, typed: Sequence[tuple[int, ...]], echo: bool
) -> list[str]:
    by_id = {e.id: e for e in now.snapshot.elements}
    element = by_id.get(eid)
    if element is None:
        return ["id_not_listed"]
    top = top_document(now.snapshot)
    if top is not None and not _inside(element, by_id, top.runtime_id):
        return ["outside_scope"]
    if _present_before(element, baseline):
        return ["not_in_delta"]
    return _excluded(element, now, typed=typed, echo=echo)


def _present_before(element: Element, baseline: Observed) -> bool:
    """§6.4: held before the baseline: the same RuntimeId under the same name, or, with no
    RuntimeId, the same role and normalised name on any element of the baseline."""
    if element.runtime_id:
        return any(
            e.runtime_id == element.runtime_id and e.name == element.name
            for e in baseline.snapshot.elements
        )
    key = (element.role, normalise_name(element.name))
    return any((e.role, normalise_name(e.name)) == key for e in baseline.snapshot.elements)


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
