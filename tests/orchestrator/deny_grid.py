"""The deny lock grid ([V2.24] item 2): the inputs `deny_tier` and `effective_risk` are locked on
before the deny rules are lifted out of `Step` for v2.

`uv run python -m tests.orchestrator.deny_grid` writes `deny_lock.json` next to this module from
the gate it runs on; it was run once on main before the refactor. A section is a cartesian
product of axes in `FIELDS` order, or a list of rows. Its outputs are one character per row: the
`deny_tier` digit, and the `effective_risk` digit at plan tier R0, or `-` where v1's `Step`
refuses the facts (a focus on a Button, a choose with no option), whose `deny_tier` is read
through a `Step` built without its checks. Special characters are built with `chr()`.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields
from pathlib import Path

from steerai.orchestrator.contracts import OPTION_PARAM, RISKS, VERBS, Risk, Step, Target
from steerai.orchestrator.gate import deny_tier, effective_risk

LOCK = Path(__file__).with_name("deny_lock.json")
FIELDS = (
    "verb",
    "role",
    "name",
    "automation_id",
    "option",
    "bound_name",
    "element_ids",
    "group_name",
)
Row = tuple[str, str, str, str, str | None, str | None, tuple[str, ...], str | None]
Axis = Sequence[str | None] | Sequence[tuple[str, ...]]
INVALID = "-"

CYR_DZE = chr(0x0405)  # looks like S
CYR_IE = chr(0x0435)  # looks like e
CYR_O = chr(0x041E)  # looks like O
CYR_BYE_I = chr(0x0456)  # looks like i
GREEK_NU = chr(0x039D)  # looks like N; its small letter folds to v
GREEK_UPSILON = chr(0x03A5)  # looks like Y; its small letter folds to u
INV_EXCL = chr(0x00A1)  # folds to i
ARABIC_ONE = chr(0x0661)  # folds to l
CGJ = chr(0x034F)  # combining grapheme joiner
HANGUL_FILLER = chr(0x3164)  # invisible letter
DOTTED_I = chr(0x0130)  # casefolds to i + combining dot above
ZWSP = chr(0x200B)
ZWJ = chr(0x200D)
RLO = chr(0x202E)
PDF = chr(0x202C)
RIGHT_QUOTE = chr(0x2019)
MODIFIER_APOSTROPHE = chr(0x02BC)
EURO = chr(0x20AC)
POUND = chr(0x00A3)
YEN = chr(0x00A5)
RUPEE = chr(0x20B9)
FULL_DOLLAR = chr(0xFF04)
SMALL_DOLLAR = chr(0xFE69)

VERB_AXIS: tuple[str, ...] = tuple(sorted(VERBS))
UIA_ROLES: tuple[str, ...] = (
    "AppBar",
    "Button",
    "Calendar",
    "CheckBox",
    "ComboBox",
    "Custom",
    "DataGrid",
    "DataItem",
    "Document",
    "Edit",
    "Group",
    "Header",
    "HeaderItem",
    "Hyperlink",
    "Image",
    "List",
    "ListItem",
    "Menu",
    "MenuBar",
    "MenuItem",
    "Pane",
    "ProgressBar",
    "RadioButton",
    "ScrollBar",
    "SemanticZoom",
    "Separator",
    "Slider",
    "Spinner",
    "SplitButton",
    "StatusBar",
    "Tab",
    "TabItem",
    "Table",
    "Text",
    "Thumb",
    "TitleBar",
    "ToolBar",
    "ToolTip",
    "Tree",
    "TreeItem",
    "Window",
)
"""Every role v1's snapshot names: `control_type_names` over the UIAutomationClient module
(41 control types, read 2026-11-14 on Windows 10 19045)."""
ALL_ROLES: tuple[str, ...] = (*UIA_ROLES, "App", "50000", "button")
"""Plus an `open` step's App, the bare id an unknown control type is named by, and a role in the
wrong case."""
DECIDING_ROLES: tuple[str, ...] = (
    "Button",
    "SplitButton",
    "MenuItem",
    "CheckBox",
    "RadioButton",
    "TabItem",
    "ComboBox",
    "ListItem",
    "List",
    "Group",
    "DataItem",
    "Hyperlink",
    "Edit",
    "Document",
    "TreeItem",
    "App",
)
"""Every role the gate's deny rules name, and one of each other kind (link, field, row)."""

WORD_NAMES: tuple[str, ...] = (
    # DENY_PATTERNS, every entry
    "Delete",
    "Remove",
    "Pay",
    "Purchase",
    "Buy",
    "Transfer",
    "Wipe",
    "Send",
    "Submit",
    "Post",
    "Publish",
    "Share",
    "Reply",
    "Forward",
    "Confirm",
    "Sign out",
    "Signout",
    "Install",
    "Uninstall",
    "Format",
    "Reset",
    "Checkout",
    "Discard",
    "Don't save",
    "Clear history",
    "Log out",
    "Log off",
    "Sign off",
    "Logout",
    "Logoff",
    "Signoff",
    "Unsubscribe",
    # CONTROL_PATTERNS, every entry
    "Check out",
    "Subscribe",
    "Join",
    "Like",
    "Follow",
    "Report",
    "Block",
    "Leave",
    "Accept",
    "Allow",
    # CONSENT_PATTERNS, every entry
    "I agree",
    "I consent",
    "Authorize",
    "Authorise",
    "I acknowledge",
    "I certify",
    # BILLING_PATTERNS, every entry
    "Billed per month",
    "Billed per year",
    "Subscription",
    "Billing",
    # the price regex
    "$20/mo",
    "Annual ($120/yr)",
    "120 " + EURO,
    POUND + "5 a week",
    YEN + "500",
    RUPEE + "99",
    FULL_DOLLAR + "120",
    SMALL_DOLLAR + "120",
    "10/month",
    "Pro / year",
    # in a phrase, in another case, split by punctuation or a digit
    "Delete chat",
    "DELETE",
    "Delete_all",
    "(Send)",
    "Send2",
    "Sign Out of all devices",
    "Check-out",
    "Like That",
    "Leave group",
    "I do not agree",
    "Monthly subscription",
    # not the word
    "Resend",
    "Deleted",
    "Inbox",
    "OK",
    "Continue",
    "Place order",
    "Plan your trip",
    "Monthly",
    "120 items",
    "$ off",
    "Lee Block",
    # lookalikes and invisible characters
    CYR_DZE + "end",
    "D" + CYR_IE + "lete chat",
    "D" + CYR_IE + ARABIC_ONE + "ete" + INV_EXCL,
    CYR_DZE + "ubm1t",
    "SE" + GREEK_NU + "D",
    "PA" + GREEK_UPSILON,
    "Send" + HANGUL_FILLER + "Now",
    "Se" + CGJ + "nd",
    "Sign" + INV_EXCL + " " + CYR_O + "ut",
    "SUBM" + DOTTED_I + "T",
    "P0ST",
    "DeIete",
    "L" + CYR_BYE_I + "ke",
    "Dele" + ZWSP + "te chat",
    "S" + ZWJ + "end",
    RLO + "Delete chat" + PDF,
    "Don" + RIGHT_QUOTE + "t save",
    "Don" + MODIFIER_APOSTROPHE + "t save",
)
SAMPLE_NAMES: tuple[str, ...] = (
    "Delete",
    "Send",
    "Sign out",
    "Like",
    "Check out",
    "I agree",
    "Billing",
    "$20/mo",
    "OK",
    "D" + CYR_IE + "lete",
)
"""One or two from each class, for every role."""
ID_FORMS: tuple[str, ...] = (
    "",
    "btnDelete",
    "delete_chat",
    "DeleteChat",
    "chat-delete-button",
    "SEND_NOW",
    "BTNDelete",
    "SUBMit",
    "send2",
    "btnOkDialog",
    "btnOk",
    "btnCheckout",
    "checkOut",
    "logOutButton",
    "sign_out",
    "signOut",
    "dontSave",
    "don_t_save",
    "clearHistory",
    "unsubscribeLink",
    "follow-button",
    "likeButton",
    "agreeTerms",
    "consent_box",
    "billingPlan",
    "per_month",
    "planPerMonth",
    "price$20",
    "Resend",
    "deleted",
    "PAYNOW",
    "De1ete",
    "DeIete",
    "btn" + CYR_DZE + "end",
    "xyz" + CYR_IE + "Delete",
    "L" + CYR_BYE_I + "keButton",
)
"""Automation-id forms: camelCase, an acronym run, underscores, hyphens, a digit, lookalikes."""
ROLE_IDS: tuple[str, ...] = ("btnDelete", "likeButton", "agreeTerms", "billingPlan")
GROUP_NAMES: tuple[str | None, ...] = (
    None,
    "",
    "Delivery speed",
    "Subscription",
    "Billing period",
    "Pay",
    "Pay $120 a year",
    "I agree to pay the monthly fee",
    "I consent to data sharing",
    "Subscribe to the newsletter",
    "Delete account",
    "$5/month",
)
GROUPED_NAMES: tuple[str, ...] = ("Yes", "Auto-renew", "Kyoto")
BOUND_NAMES: tuple[str | None, ...] = (None, "", "Amir Khan", "Delete Me", "Lee Like")
ELEMENT_IDS: tuple[tuple[str, ...], ...] = (
    (),
    ("",),
    ("btnOk",),
    ("btnDeleteAccount",),
    ("btnOk", "follow-button"),
    ("agree_terms", "billing_plan"),
)
OPTION_ROLES: tuple[str, ...] = (
    "ComboBox",
    "ListItem",
    "Button",
    "RadioButton",
    "CheckBox",
    "Edit",
)
OPTION_NAMES: tuple[str, ...] = ("Plan", "Delete account", "Answer")
OPTIONS: tuple[str | None, ...] = (
    None,
    "",
    "Now",
    "Yes",
    "Premium ($20/mo)",
    "Delete selected",
    "I agree",
    "Subscribe",
    "Monthly billing",
)
OPTION_GROUPS: tuple[str | None, ...] = (None, "I consent to data sharing")


def _axes(
    *,
    verbs: Sequence[str] = VERB_AXIS,
    roles: Sequence[str],
    names: Sequence[str],
    ids: Sequence[str] = ("",),
    options: Sequence[str | None] = (None,),
    bound: Sequence[str | None] = (None,),
    element_ids: Sequence[tuple[str, ...]] = ((),),
    groups: Sequence[str | None] = (None,),
) -> dict[str, Axis]:
    return {
        "verb": list(verbs),
        "role": list(roles),
        "name": list(names),
        "automation_id": list(ids),
        "option": list(options),
        "bound_name": list(bound),
        "element_ids": list(element_ids),
        "group_name": list(groups),
    }


class _Rows:
    """Explicit rows, one per call of a deny test on main."""

    def __init__(self) -> None:
        self.rows: list[Row] = []

    def add(
        self,
        verb: str,
        role: str,
        name: str,
        automation_id: str = "",
        *,
        option: str | None = None,
        bound: str | None = None,
        element_ids: tuple[str, ...] = (),
        group: str | None = None,
    ) -> None:
        self.rows.append((verb, role, name, automation_id, option, bound, element_ids, group))


def _existing() -> list[Row]:
    """The inputs of the deny tests on main (test_deny, test_deny_classes, test_deny_homoglyphs,
    test_select_tier, test_choose_verb, test_capability_verbs), one row per call."""
    rows = _Rows()
    _deny_test_rows(rows.add)
    _homoglyph_rows(rows.add)
    _value_rows(rows.add)
    return rows.rows


def _deny_test_rows(add: Callable[..., None]) -> None:
    for name in (
        "Confirm",
        "Sign out",
        "Sign Out of all devices",
        "Install",
        "Uninstall",
        "Format",
        "Reset",
        "Dele" + ZWSP + "te chat",
        "S" + ZWJ + "end",
        RLO + "Delete chat" + PDF,
        "Sign" + ZWSP + "out",
        "Delete_all",
        "Send2",
        "SEND",
        "Resend",
        "Checkout",
        "Discard",
        "Don't Save",
        "Clear history",
        "Log off",
        "Sign off",
        "Join",
        "Report",
        "Accept all",
        "Continue",
        "Place order",
        "Logout",
        "LOGOFF",
        "signoff",
        "Like",
    ):
        add("click", "Button", name)
    for code in (0x27, 0x2019, 0x02BC, 0xFF07, 0x60, 0xB4):
        add("click", "Button", f"Don{chr(code)}t save")
    for automation_id in (
        "btnDelete",
        "delete_chat",
        "DeleteChat",
        "chat-delete-button",
        "SEND_NOW",
        "btnOkDialog",
        "btnCheckout",
        "logOutButton",
        "btn" + CYR_DZE + "end",
        "xyz" + CYR_IE + "Delete",
        "BTNDelete",
    ):
        add("click", "Button", "OK", automation_id)
    add("delete", "ListItem", "Amir Khan", "row-7")
    add("click", "Button", "OK", element_ids=("btnDeleteAccount",))
    add("click", "Button", "OK", element_ids=("btnOk", ""))
    add("delete", "Button", "OK", element_ids=("btnOk",))
    for name in (
        "Check out this video",
        "Drake - Like That (Official)",
        "Unsubscribe",
        "Checkout",
        "Log out",
        "L" + CYR_BYE_I + "ke That",
    ):
        add("click", "Hyperlink", name)
    add("click", "Hyperlink", "Watch", "follow-button")
    add("click", "Hyperlink", "Watch", "L" + CYR_BYE_I + "keButton")
    add("type", "Edit", "Check-out")
    add("focus", "Edit", "Check-out")
    add("click", "ListItem", "Lee Block")


def _homoglyph_rows(add: Callable[..., None]) -> None:
    for name in (
        CYR_DZE + "end",
        "D" + CYR_IE + "lete chat",
        CYR_DZE + "end" + INV_EXCL,
        "D" + CYR_IE + ARABIC_ONE + "ete" + INV_EXCL,
        "D" + CYR_IE + "1ete",
        CYR_DZE + "ubm1t",
        "SE" + GREEK_NU + "D",
        "PA" + GREEK_UPSILON,
        "U" + GREEK_NU + "INSTA" + ARABIC_ONE + "L",
        "Send" + HANGUL_FILLER + "Now",
        "Se" + CGJ + "nd",
        "Se" + chr(0x01) + "nd",
        "Sign" + INV_EXCL + " " + CYR_O + "ut",
        "SUBM" + DOTTED_I + "T",
        "P0ST",
        "De1ete",
        "DeIete",
        chr(0x0420) + chr(0x0430) + chr(0x0443),
        "L" + CYR_BYE_I + "ke",
        "".join(chr(code) for code in (0xFF33, 0xFF45, 0xFF4E, 0xFF44)),
        "".join(chr(0x1D400 + ord(c) - ord("A")) for c in "SEND"),
        "Del" + ZWSP + "ete",
        "Inbox",
        "Sara",
        "Sender list",
        INV_EXCL * 3,
        chr(0x00D7),
        chr(0x0307),
    ):
        add("click", "Button", name)


def _value_rows(add: Callable[..., None]) -> None:
    pay = "I agree to pay the monthly fee"
    share = "I consent to data sharing"
    add("select", "RadioButton", "Yes", group=pay)
    add("select", "RadioButton", "Allow", group=share)
    add("select", "RadioButton", "Deny", group=share)
    add("select", "RadioButton", "Yes", group="Subscribe to the newsletter")
    for group in (None, "", "Delivery speed"):
        add("select", "RadioButton", "Deny", group=group)
        add("select", "RadioButton", "Yes", group=group)
    add("select", "TabItem", "Yes", group=pay)
    add("select", "TabItem", "Delete")
    add("check", "CheckBox", "Email receipts", group=pay)
    add("check", "CheckBox", "Email receipts", group="Billing emails")
    for name in ("I agree", "I do not agree", "I consent", "I certify this"):
        add("select", "RadioButton", name)
    add("select", "TabItem", "I agree")
    for name in (
        "Annual ($120/yr)",
        "$120",
        "$ 120",
        "120$",
        "120" + EURO,
        "120 " + EURO,
        EURO + "9.99",
        POUND + "5 a week",
        YEN + "500",
        RUPEE + "99",
        FULL_DOLLAR + "120",
        SMALL_DOLLAR + "120",
        FULL_DOLLAR + chr(0xFF11) + chr(0xFF12) + chr(0xFF10),
        "10/mo",
        "Pro / yr",
        "Billed per month",
        "Billed per year",
        "Monthly subscription",
        "Billing address same as shipping",
    ):
        add("select", "RadioButton", name)
        add("check", "CheckBox", name)
    for name in (
        "Monthly",
        "Annual",
        "Plan your trip",
        "Basic plan",
        "120 items",
        "$ off",
        "Mode",
        "Year in review",
        "Momentum",
        "/ yearly digest",
    ):
        add("select", "RadioButton", name)
    add("select", "RadioButton", "Monthly", group="Billing period")
    add("select", "RadioButton", "Annual", group="Pay $120 a year")
    add("select", "TabItem", "$120")
    add("click", "Button", "$120")
    for name in ("Subscribe", "Join", "Follow", "Accept", "Check out", "Kyoto", "Delete all"):
        add("select", "ListItem", name)
    add("click", "ListItem", "Subscribe")
    add("click", "ListItem", "I agree")
    add("select", "ListItem", "I agree")
    add("select", "ListItem", "Premium $20/mo")
    add("select", "ListItem", "Monthly")
    add("select", "ListItem", "Annual", group="Billing period")
    add("select", "ListItem", "Yes", group=pay)
    add("select", "ListItem", "Yes", group="Do you consent?")
    add("select", "ListItem", "Follow", group="Destinations")
    add("select", "ListItem", "Kyoto", group="Destinations")
    add("choose", "ComboBox", "Plan", option="Premium ($20/mo)")
    add("choose", "ComboBox", "Bulk action", option="Delete selected")
    add("choose", "ComboBox", "Delete account", option="Now")
    add("choose", "ComboBox", "Answer", option="Yes", group=share)
    for option in ("Subscribe", "I agree", "Monthly billing"):
        add("choose", "ComboBox", "Answer", option=option)
    add("focus", "ComboBox", "Size", group=share)
    add("check", "CheckBox", "I agree to share my data")
    add("check", "Button", "I agree to share my data")
    add("uncheck", "Button", "I agree to share my data")
    add("select", "RadioButton", "Annual plan $99/year")
    add("select", "DataItem", "Annual plan $99/year")
    add("check", "Button", "Dark mode")
    add("select", "DataItem", "Order 1001")
    add("click", "Button", "I agree")
    add("check", "Button", "Terms", "agreeTerms")
    for group in ("Subscription", "Pay"):
        add("check", "Button", "Auto-renew", group=group)
    add("check", "CheckBox", "Auto-renew", group="Subscription")
    add("select", "TabItem", "Auto-renew", group="Subscription")


def sections() -> dict[str, dict[str, Axis] | list[Row]]:
    """The grid's inputs by section name: axes for a product, or a list of rows."""
    return {
        "words": _axes(roles=DECIDING_ROLES, names=WORD_NAMES),
        "roles": _axes(roles=ALL_ROLES, names=SAMPLE_NAMES),
        "ids": _axes(roles=DECIDING_ROLES, names=("OK",), ids=ID_FORMS),
        "role_ids": _axes(roles=ALL_ROLES, names=("OK",), ids=ROLE_IDS),
        "groups": _axes(roles=DECIDING_ROLES, names=GROUPED_NAMES, groups=GROUP_NAMES),
        "bound": _axes(
            roles=DECIDING_ROLES, names=("OK",), bound=BOUND_NAMES, element_ids=ELEMENT_IDS
        ),
        "options": _axes(
            roles=OPTION_ROLES, names=OPTION_NAMES, options=OPTIONS, groups=OPTION_GROUPS
        ),
        "existing": _existing(),
    }


Section = Mapping[str, Sequence[object]] | Sequence[Sequence[object]]
"""Axes by field name, or rows: as `sections` builds them or as the lock file holds them."""


def expand(section: Section) -> list[Row]:
    """A section's rows: its list, or the product of its axes in `FIELDS` order."""
    if isinstance(section, Mapping):
        return [row_of(values) for values in itertools.product(*(section[f] for f in FIELDS))]
    return [row_of(values) for values in section]


def row_of(values: Sequence[object]) -> Row:
    """One row's facts, checked: JSON gives lists and nulls where the grid has tuples and None."""
    verb, role, name, automation_id, option, bound, element_ids, group = values
    if not (isinstance(verb, str) and isinstance(role, str) and isinstance(name, str)):
        raise TypeError("verb, role and name are strings")
    if not isinstance(automation_id, str):
        raise TypeError("automation_id is a string")
    if not all(v is None or isinstance(v, str) for v in (option, bound, group)):
        raise TypeError("option, bound_name and group_name are strings or null")
    if not isinstance(element_ids, tuple | list) or not all(
        isinstance(i, str) for i in element_ids
    ):
        raise TypeError("element_ids is a list of strings")
    return (
        verb,
        role,
        name,
        automation_id,
        option if isinstance(option, str) else None,
        bound if isinstance(bound, str) else None,
        tuple(str(i) for i in element_ids),
        group if isinstance(group, str) else None,
    )


def checked_step(row: Row, risk: Risk = "R0") -> Step | None:
    """The row as v1's `Step` with every check, or None where `Step` refuses it."""
    verb, role, name, automation_id, option, _, _, _ = row
    try:
        return Step(
            id="s1",
            subgoal="g1",
            verb=verb,
            target=Target(role=role, name=name, automation_id=automation_id),
            text="x" if verb == "type" else None,
            risk=risk,
            params={} if option is None else {OPTION_PARAM: option},
        )
    except ValueError:
        return None


def raw_step(row: Row) -> Step:
    """The row as a `Step` built without `__post_init__`: the facts v1's checks refuse (a focus
    on a Button, a choose with no option, an option on a click) still reach `deny_tier`."""
    verb, role, name, automation_id, option, _, _, _ = row
    values: dict[str, object] = {
        "id": "s1",
        "subgoal": "g1",
        "verb": verb,
        "target": Target(role=role, name=name, automation_id=automation_id),
        "text": None,
        "risk": "R0",
        "params": {} if option is None else {OPTION_PARAM: option},
        "expect": None,
        "preapproved_by_request": False,
        "text_provenance": "verbatim",
        "path": None,
        "window": None,
    }
    if set(values) != {f.name for f in fields(Step)}:
        raise TypeError("raw_step sets every Step field")
    step = object.__new__(Step)
    for key, value in values.items():
        object.__setattr__(step, key, value)
    return step


def step_deny(row: Row) -> Risk:
    """`deny_tier` through a `Step`: the checked one where v1 builds it, else the raw one."""
    _, _, _, _, _, bound, element_ids, group = row
    step = checked_step(row) or raw_step(row)
    return deny_tier(step, bound, element_ids, group_name=group)


def step_effective(row: Row) -> str:
    """`effective_risk` of the checked `Step` at plan tier R0 (the verb floor applies), or `-`."""
    _, _, _, _, _, bound, element_ids, group = row
    step = checked_step(row)
    if step is None:
        return INVALID
    return str(RISKS.index(effective_risk(step, bound, element_ids, group_name=group)))


def _encode(section: Section) -> dict[str, object]:
    rows = expand(section)
    out: dict[str, object] = (
        {"axes": {k: list(v) for k, v in section.items()}}
        if isinstance(section, Mapping)
        else {"rows": [list(r) for r in rows]}
    )
    out["count"] = len(rows)
    out["deny"] = "".join(str(RISKS.index(step_deny(r))) for r in rows)
    out["effective"] = "".join(step_effective(r) for r in rows)
    return out


def main() -> None:
    encoded = {name: _encode(section) for name, section in sections().items()}
    lock = {
        "about": "deny_tier and effective_risk on main before the [V2.24] item 2 refactor",
        "fields": list(FIELDS),
        "risks": list(RISKS),
        "total": sum(int(str(s["count"])) for s in encoded.values()),
        "sections": encoded,
    }
    text = json.dumps(lock, ensure_ascii=True, indent=1) + "\n"
    LOCK.write_bytes(text.encode("ascii"))


if __name__ == "__main__":
    main()
