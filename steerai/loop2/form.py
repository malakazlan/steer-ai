"""The form around a typed field ([V2.22], [V2.30]): one scope for the submit rule and for the
query conditions' form facts.

The form is the field's [V2.22] scope: the elements under the field's parent container, and the
elements that share its nearest landmark. A fact needed to tell (a broken parent chain, an unread
landmark map) is missing, and a missing fact reads as inside the form, so the facts fail closed.
When the field's parent is the Document or its nearest landmark is Main, the form is most of the
page ([V2.30] (1): counted, not narrowed).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from steerai.orchestrator.gate import DenyFacts, billing_reads, deny_tier_of, group_name
from steerai.world.effects import Observed
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
ACTING_ROLES: Final = frozenset({"Button", "Hyperlink", "MenuItem", "SplitButton"})
"""[V2.26] (3): Enter by the role it goes to: a field is classified, an acting role is a click
(deny words and the pending-text rule), a state-set role is (1), anything else is R2."""


def fitting_verb(role: str) -> str | None:
    """[V2.30] (3): the verb that fits a role, for reading its deny facts without an action: a
    field is typed into, an acting role is clicked, a state-set role takes its state verb. None
    for a role no verb acts on (text, an image, a container): it is not read."""
    if role in ENTER_FIELDS:
        return "type"
    if role in ACTING_ROLES:
        return "click"
    return STATE_SET_VERB.get(role)


def beside(observed: Observed, field: tuple[int, ...], target: Element) -> bool | None:
    """[V2.22] the target is under the field's parent container, or shares its nearest landmark;
    None when a fact needed to tell is missing (a broken parent chain, or the landmark map or
    either element in it not read)."""
    by_id = {e.id: e for e in observed.snapshot.elements}
    typed = next(e for e in observed.snapshot.elements if e.runtime_id == field)
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
    both facts true."""
    snapshot = observed.snapshot
    if all(e.runtime_id != field for e in snapshot.elements):
        return True, True
    secure = risky = False
    for element in snapshot.elements:
        if element.runtime_id == field or beside(observed, field, element) is False:
            continue
        secure = secure or element.secure or element.id in snapshot.unread_secure
        risky = risky or _risky(observed, element)
    return secure, risky


def _risky(observed: Observed, element: Element) -> bool:
    verb = fitting_verb(element.role)
    if verb is None:
        return False
    facts = DenyFacts(
        verb=verb,
        role=element.role,
        name=element.name,
        automation_id=element.automation_id,
        group_name=group_name(observed.snapshot, element),
    )
    return billing_reads(facts) or deny_tier_of(facts) == "R3"
