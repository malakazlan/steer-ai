"""The form around a typed field ([V2.22], [V2.30]): one scope for the submit rule and for the
query conditions' form facts.

The form is the field's [V2.22] scope: the elements under the field's parent container, and the
elements that share its nearest landmark. A fact needed to tell (a broken parent chain, an unread
landmark map) is missing, and a missing fact reads as inside the form, so the facts fail closed.
When the field's parent is the Document or its nearest landmark is Main, the form is most of the
page ([V2.30] (1): counted, not narrowed).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from functools import lru_cache
from typing import Final

from steerai.orchestrator.gate import (
    DenyFacts,
    billing_reads,
    deny_tier_of,
    group_name,
    may_read,
)
from steerai.world.effects import Observed, top_document
from steerai.world.elements import Element

STATE_SET_VERB: Final[Mapping[str, str]] = {
    "ListItem": "select",
    "TabItem": "select",
    "TreeItem": "select",
    "CheckBox": "check",
    "RadioButton": "check",
}
"""[V2.26] (1): the state verb a Space or an Enter performs on each state-set role; the press
takes that verb's deny scoping (consent and billing read a selected option, as `select` does)."""
ENTER_FIELDS: Final = frozenset({"Edit", "ComboBox"})
RISKY_MEMO: Final = 4096
"""How many (role, name, automation id, group) readings the risky pass remembers (#314): the
type-time and the dispatch-time scans of one form read the same elements."""
MAX_FORM_MEMBERS: Final = 3000
"""[V2.35] (1): a form with more members than this is not scanned; both its facts read true (the
budget is approved for pages up to 3000 elements). Counted, never timed, so CI is deterministic."""
MAX_RISKY_READS: Final = 300
"""[V2.35] (1): more members than this past the prefilter (10% of the cap, the budget's worst
measured case) makes the form risky without running the gate on the rest."""
GROUPED_CHOICES: Final = frozenset({"RadioButton", "CheckBox"})
"""The roles whose group name the deny classes read ([v2.77], W3-03)."""
ACTING_ROLES: Final = frozenset({"Button", "Hyperlink", "MenuItem", "SplitButton"})
"""[V2.26] (3): Enter by the role it goes to: a field is classified, an acting role is a click
(deny words and the pending-text rule), a state-set role is (1), anything else is R2."""


CHOOSABLE: Final = "ComboBox"


def fitting_verbs(role: str) -> tuple[str, ...]:
    """[V2.30] (3): the verbs that fit a role, for reading its deny facts without an action. A
    field is typed into (a ComboBox is also chosen from: billing reads it only under `choose`);
    a state-set role takes its state verb; every other role is read as clicked, since deny
    words read names and automation ids on every role and a page can make anything clickable
    (principal review of #312)."""
    if role == CHOOSABLE:
        return ("type", "choose")
    if role in ENTER_FIELDS:
        return ("type",)
    if role in STATE_SET_VERB:
        return (STATE_SET_VERB[role],)
    return ("click",)


def beside(observed: Observed, field: tuple[int, ...], target: Element) -> bool | None:
    """[V2.22] the target is under the field's parent container, or shares its nearest landmark;
    None when a fact needed to tell is missing (a broken parent chain, or the landmark map or
    either element in it not read)."""
    by_id = {e.id: e for e in observed.snapshot.elements}
    typed = next(e for e in observed.snapshot.elements if e.runtime_id == field)
    return _beside(observed, by_id, typed, target)


def scope_fields(observed: Observed, target: Element) -> tuple[Element, ...]:
    """[V2.64] Two: the fields (Edit, ComboBox) whose [V2.22] scope holds a click's target, in
    the page's top Document. A missing fact reads as inside; a field proven outside the top
    Document (the browser frame's address bar) is never in it."""
    snapshot = observed.snapshot
    by_id = {e.id: e for e in snapshot.elements}
    top = top_document(snapshot)
    return tuple(
        e
        for e in snapshot.elements
        if e.role in ENTER_FIELDS
        and e.runtime_id != target.runtime_id
        and (top is None or _under(e, top.id, by_id) is not False)
        and _beside(observed, by_id, e, target) is not False
    )


def _beside(
    observed: Observed, by_id: Mapping[int, Element], typed: Element, target: Element
) -> bool | None:
    field = typed.runtime_id
    if typed.parent_id is not None:
        under = _under(target, typed.parent_id, by_id)
        if under is not False:
            return under
    marks = observed.landmarks
    if marks is None or field not in marks or target.runtime_id not in marks:
        return None
    mine, theirs = marks[field], marks[target.runtime_id]
    return mine is not None and theirs is not None and mine.runtime_id == theirs.runtime_id


def _under(element: Element, ancestor: int, by_id: Mapping[int, Element]) -> bool | None:
    """True when `ancestor` is above `element`; None when the chain breaks (a parent id that is
    not in the snapshot): unknown, never "elsewhere" (principal review 3)."""
    seen: set[int] = set()
    parent = element.parent_id
    while parent is not None and parent not in seen:
        if parent == ancestor:
            return True
        current = by_id.get(parent)
        if current is None:
            return None
        seen.add(parent)
        parent = current.parent_id
    return False


def form_facts(observed: Observed, field: tuple[int, ...]) -> tuple[bool, bool]:
    """[V2.30] (2) and (3): (`secure_in_form`, `risky_in_form`) for the form around `field` in
    `observed`. Secure: an element's secure flag is set or was not read. Risky: an element gets
    the billing class, or `deny_tier_of` raises it to R3, for the verb that fits its role. An
    element whose place is unknown counts as inside; a field that is not in the snapshot makes
    both facts true.

    Cost (#314, [V2.32]): the secure pass is a flag read per member; the risky pass stops at the
    first risky member, skips a member no pattern can read (`gate.may_read`, locked against
    `deny_tier_of`), and remembers each (role, name, automation id, group) it decided."""
    snapshot = observed.snapshot
    typed = next((e for e in snapshot.elements if e.runtime_id == field), None)
    if typed is None:
        return True, True
    by_id = {e.id: e for e in snapshot.elements}
    members = [
        e
        for e in snapshot.elements
        if e.runtime_id != field and _beside(observed, by_id, typed, e) is not False
    ]
    if len(members) > MAX_FORM_MEMBERS:
        return True, True  # [V2.35] (1): over budget means true, never a fact left unread
    secure = any(e.secure or e.id in snapshot.unread_secure for e in members)
    return secure, _any_risky(observed, by_id, members)


def _any_risky(observed: Observed, by_id: Mapping[int, Element], members: list[Element]) -> bool:
    """The risky pass: the first risky member decides it. A choice whose group is not in the
    snapshot is risky (#319 review S1-07: its group name is unread, never "no group"). More
    than `MAX_RISKY_READS` members past the prefilter is risky ([V2.35] (1))."""
    reads = 0
    for element in members:
        orphan = element.parent_id is not None and element.parent_id not in by_id
        if element.role in GROUPED_CHOICES and orphan:
            return True
        group = group_name(observed.snapshot, element) or ""
        passed, risky = _reads_risky(element.role, element.name, element.automation_id, group)
        if risky:
            return True
        reads += passed
        if reads > MAX_RISKY_READS:
            return True
    return False


@lru_cache(maxsize=RISKY_MEMO)
def _reads_risky(role: str, name: str, automation_id: str, group: str) -> tuple[bool, bool]:
    """(the prefilter let it through, it is risky) for one reading of an element."""
    texts = DenyFacts(
        verb="click", role=role, name=name, automation_id=automation_id, group_name=group or None
    )
    if not may_read(texts):
        return False, False
    for verb in fitting_verbs(role):
        facts = dataclasses.replace(texts, verb=verb)
        if billing_reads(facts) or deny_tier_of(facts) == "R3":
            return True, True
    return True, False


def clear_memo() -> None:
    """Forget every remembered reading (tests start cold)."""
    _reads_risky.cache_clear()
