"""The code verifier's evidence rule for step 3 ([V2.3], [V2.11] decision 7): cited evidence must
be inside the scope Document; a tab name, the address bar or another Document never is."""

from __future__ import annotations

from steerai.loop2.decision import Claim
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
