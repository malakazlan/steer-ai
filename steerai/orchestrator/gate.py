"""The safety gate in bootstrap mode (design 5.13; [v0.4] bootstrap; [v1.2] bound decisions).

Until calibration exists this is the only mode: R0/R1 steps are approved with strict T1 (a
predicate that is weak after the template rung is a block, never a weaker check), R2/R3 steps
always confirm through the `Confirmer`, including text the request pre-approved. Deny patterns
on the target's name raise the tier and never lower it. A step aimed at Steer's own process is
blocked before anything else. Every approval or confirmation is a `GateDecision` bound to one
action key and consumed by one execution; an answer counts only for the open ask's decision id,
only as the exact token, so a stray Enter, a late reply or a sloppy "y" never acts. No answer
is `gate_pending_user`: nothing runs. The ask carries tokens and the parameters for the
confirmer to render; names stay out of its repr.
"""

from __future__ import annotations

import re
import urllib.parse
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from steerai.action.launch import SITES, LaunchError, expected_title, site_url
from steerai.errors import ErrorCode, SteerError
from steerai.goal_match import ASKING
from steerai.orchestrator.contracts import (
    CHOOSE_ROLE,
    OPTION_PARAM,
    RISKS,
    GateDecision,
    Risk,
    Step,
    template_verb,
    verbatim_in,
)
from steerai.orchestrator.identity import Binding, CandidateLine, lines_for
from steerai.verify.deny_skeleton import camel_split, holds
from steerai.verify.predicates import Appeared, Predicate, Present, Within, comparable, presents
from steerai.verify.strength import Params, strength, strengthen, template_for
from steerai.verify.t1 import redact
from steerai.world.elements import Element
from steerai.world.model import Snapshot, normalise_name

DENY_PATTERNS: Mapping[str, Risk] = {
    "delete": "R3",
    "remove": "R3",
    "pay": "R3",
    "purchase": "R3",
    "buy": "R3",
    "transfer": "R3",
    "wipe": "R3",
    "send": "R2",
    "submit": "R2",
    "post": "R2",
    "publish": "R2",
    "share": "R2",
    "reply": "R2",
    "forward": "R2",
    "confirm": "R2",
    "sign out": "R2",
    "signout": "R2",
    "install": "R3",
    "uninstall": "R3",
    "format": "R3",
    "reset": "R3",
    # Phase 3 classes (5.13 1a [v2.17], deny-classes gate): every role, raise only
    "checkout": "R3",
    "discard": "R3",
    "don t save": "R3",  # "Don't Save", any apostrophe, as the word matcher splits it
    "clear history": "R3",
    "log out": "R2",
    "log off": "R2",
    "sign off": "R2",
    "logout": "R2",
    "logoff": "R2",
    "signoff": "R2",
    "unsubscribe": "R2",  # every role: an email Unsubscribe link acts (DC-07)
}
"""5.13 1a: words (or word sequences) in a target's name or automation id that raise the step's
tier. Matched on whole words of the comparable form (NFKC, format characters removed,
case-folded), so punctuation or a zero-width character never hides a word (RN-04)."""
CONTROL_PATTERNS: Mapping[str, Risk] = {
    "check out": "R2",
    "subscribe": "R2",
    "join": "R2",
    "like": "R2",
    "follow": "R2",
    "report": "R2",
    "block": "R2",
    "leave": "R2",
    "accept": "R2",
    "allow": "R2",
}
"""5.13 1a [v2.17], ruling (a): words that are also common in titles ("Like That", "Check out
this video") raise a target's name only on a control role, and its automation id on any role. A
raised click has no strength template and blocks `weak_predicate` (DC-19/20), so a title hit
would fail a navigation click outright."""
CONSENT_PATTERNS: Mapping[str, Risk] = {
    "agree": "R2",
    "consent": "R2",
    "authorize": "R2",
    "authorise": "R2",
    "acknowledge": "R2",
    "certify": "R2",
}
"""5.13 [v2.58] the consent class (widget-verbs WV-08): words that make ticking a CheckBox or
choosing a radio option a statement the person signs, read on the name and automation id of a
CheckBox or a RadioButton only (W3 verdict ruling (a): "I agree / I do not agree" radios)."""
CONSENT_ROLES: frozenset[str] = frozenset({"CheckBox", "RadioButton"})
BILLING_PATTERNS: Mapping[str, Risk] = {
    "per month": "R2",
    "per year": "R2",
    "subscription": "R2",
    "billing": "R2",
}
"""W3-02: whole words that make a CheckBox or a RadioButton a billing choice, read on its name
and its group name. "plan" alone is not one ("Plan your trip"); prices are `_BILLED`."""
_BILLED = re.compile(r"[$€£¥₹]\s?\d|\d\s?[$€£¥₹]|/\s?(?:mo|month|yr|year)\b")
"""W3-02 on the comparable form (NFKC folds a full-width or small dollar sign to $): a currency
symbol ($, euro, pound, yen, rupee) on either side of a digit, with or without a space, and a
slash period ("/mo", "/yr", and their long forms)."""
BILLING_ROLES: frozenset[str] = frozenset({"CheckBox", "RadioButton"})
GROUP_ROLE = "Group"
GROUPED_ROLE = "RadioButton"
"""W3-03: the role whose nearest Group ancestor's name is read through the deny, control and
consent patterns (Chrome 156 names it after a fieldset's legend or a radiogroup's aria-label)."""
CONTROL_ROLES: frozenset[str] = frozenset(
    {"Button", "SplitButton", "MenuItem", "CheckBox", "RadioButton"}
)
SELECTED_CONTROL_ROLE = "ListItem"
LISTBOX_PARENT_ROLE = "List"
"""A listbox option's container, whose name raises its options (#206 review)."""
"""W3b: a listbox option is a control only under `select`, so CONTROL_PATTERNS read its name then
("Subscribe" as an option asks at R2); a clicked ListItem (a chat row) keeps ruling (a)."""
_RISK_ORDER = {risk: index for index, risk in enumerate(RISKS)}
_WHICH = re.compile(r"([1-9][0-9]{0,2}):(w-[0-9a-f]{12})")
_WORD = re.compile(r"[^\W\d_]+")
"""Whole words of letters: punctuation, digits or underscores around a word ("Delete,",
"(Send)", "Delete_all", "Send2") never hide it."""
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
Outcome = Literal["approve", "confirm", "block", "pending"]


_APOSTROPHE_LETTERS = {0x02B9: " ", 0x02BB: " ", 0x02BC: " ", 0x02BD: " "}
"""Modifier-letter apostrophes (U+02B9, U+02BB-U+02BD) are word characters to the regex, so
"Don" + U+02BC + "t" would stay one word; matching treats them as separators (hunter DC-15)."""


def _words(text: str) -> list[str]:
    return _WORD.findall(comparable(text).translate(_APOSTROPHE_LETTERS))


_TOKEN = re.compile(r"[^\W_]+")
"""Letters and digits: a number in an `open`'s content is content too (review item 1)."""


def _tokens(text: str) -> list[str]:
    return [token.casefold() for token in _TOKEN.findall(comparable(text))]


def _id_words(automation_id: str) -> list[str]:
    # btnDelete, delete_chat, chat-delete-button: split on case changes, then on non-words
    return _words(_CAMEL.sub(" ", automation_id))


def _contains(words: list[str], pattern: str) -> bool:
    wanted = pattern.split()
    if len(wanted) == 1:
        return wanted[0] in words
    return any(words[i : i + len(wanted)] == wanted for i in range(len(words) - len(wanted) + 1))


def _declined(
    answer: str, risk: Risk, decision_id: str, reasons: tuple[str, ...] = ()
) -> GateResult:
    """Anything but a yes: a no blocks, the kill switch blocks (TK-23), silence pauses. Why a
    step below R2 asked rides along as closed tokens, never text."""
    why: dict[str, Any] = {"ask_reasons": list(reasons)} if reasons else {}
    if answer in ("no", f"no:{decision_id}"):
        return _block("user_declined", risk, decision_id=decision_id, **why)
    if answer == "killed":
        return _block("killed", risk, decision_id=decision_id, **why)
    return GateResult(
        outcome="pending",
        decision=None,
        code=ErrorCode.GATE_PENDING_USER,
        evidence={"risk": risk, "decision_id": decision_id, "detail": "unanswered", **why},
    )


def ask_reasons(step: Step, goal: str, verdict: str | None = None) -> tuple[str, ...]:
    """Why a step asks whatever its tier (5.13 1b, step 2 [v2.27]): text typed into a page
    that is not the person's own words, and a goal-match verdict other than `serves`. The
    label is never trusted alone: with the goal known, text absent from it is generated
    whatever the plan says. The tier, and so the verification, is unchanged: the person
    answers intent, not the effect's proof ([v2.17] ruling (b))."""
    reasons: list[str] = []
    if step.verb == "type" and step.text is not None:
        generated = step.text_provenance != "verbatim" or (
            bool(goal) and not verbatim_in(step.text, goal)
        )
        if generated:
            reasons.append("generated_text")
    if verdict in ASKING:
        reasons.append(f"goal_{verdict}")
    return tuple(reasons)


def effective_risk(
    step: Step,
    bound_name: str | None = None,
    element_ids: Sequence[str] = (),
    *,
    group_name: str | None = None,
) -> Risk:
    """The plan's tier raised by the deny patterns on the target's name and automation id,
    on the bound element's name part for a person target (PT-31), on the automation ids
    of the elements the step resolves to (#84: the page owns them, the plan may name none),
    and on a radio's group name (W3-03), never lowered."""
    raised = deny_tier(step, bound_name, element_ids, group_name=group_name)
    return raised if _RISK_ORDER[raised] > _RISK_ORDER[step.risk] else step.risk


def deny_tier(
    step: Step,
    bound_name: str | None = None,
    element_ids: Sequence[str] = (),
    *,
    group_name: str | None = None,
) -> Risk:
    """The tier the deny, control, consent and billing patterns alone raise the step to,
    without the plan's tier or the verb floor: R0 when nothing reads. `group_name` is the name
    of the resolved element's nearest ancestor when that is a Group: read for a RadioButton
    through every class (W3-03), for a CheckBox through billing only (W3-02). A choose reads
    every class on its option, its ComboBox's name and that group's name (W4-03). A select on a
    listbox option reads every class on the option's name and its List's name too (W3b, #206
    review: a `<select size>` is the same control as a `<select>`, so an option's tier never
    depends on the verb the planner picked)."""
    risk: Risk = "R0"
    if step.target is None:
        return risk
    if step.verb == "choose" and step.target.role == CHOOSE_ROLE:
        return _chosen_tier(step, bound_name, element_ids, group_name)
    if step.verb == "select" and step.target.role == SELECTED_CONTROL_ROLE:
        return _listed_tier(step, bound_name, element_ids, group_name)
    role = step.target.role
    group = [group_name] if group_name and role in BILLING_ROLES else []
    grouped = group if role == GROUPED_ROLE else []
    names = [step.target.name, *([bound_name] if bound_name else []), *grouped]
    ids = [i for i in (step.target.automation_id, *element_ids) if i]
    controlled = [step.target.name, *grouped] if role in CONTROL_ROLES else []
    consenting = role in CONSENT_ROLES
    billed = [step.target.name, *group] if role in BILLING_ROLES else []
    if any(_BILLED.search(comparable(name)) for name in billed):
        risk = "R2"
    classes: tuple[tuple[Mapping[str, Risk], list[str], list[str]], ...] = (
        (DENY_PATTERNS, names, ids),
        (CONTROL_PATTERNS, controlled, ids),
        (CONSENT_PATTERNS, names if consenting else [], ids if consenting else []),
        (BILLING_PATTERNS, billed, []),
    )
    for patterns, scope, scope_ids in classes:
        for pattern, tier in patterns.items():
            if _RISK_ORDER[tier] <= _RISK_ORDER[risk]:
                continue
            if any(_reads(name, pattern) for name in scope) or any(
                _reads_id(automation_id, pattern) for automation_id in scope_ids
            ):
                risk = tier
    return risk


def _listed_tier(
    step: Step, bound_name: str | None, element_ids: Sequence[str], group: str | None
) -> Risk:
    """A select on a listbox option is the option it picks (#206 review, as W4-03 for a
    choose): deny, control, consent and billing read the option's name, the bound name and the
    container List's name ("Premium $20/mo" is R2, "Yes" under a List "I agree to pay" R3);
    the automation ids through every class but billing. A clicked ListItem keeps the [v2.17]
    ruling (a)."""
    target = step.target
    if target is None:  # the caller checked it; kept for the type checker
        return "R0"
    names = [target.name, *(n for n in (bound_name, group) if n)]
    ids = [i for i in (target.automation_id, *element_ids) if i]
    return _every_class(names, ids)


def _every_class(names: list[str], ids: list[str]) -> Risk:
    """Every pattern class on `names`, the ids through every class but billing, the price
    regex on the comparable names; raise-only from R0."""
    risk: Risk = "R2" if any(_BILLED.search(comparable(name)) for name in names) else "R0"
    classes: tuple[tuple[Mapping[str, Risk], list[str]], ...] = (
        (DENY_PATTERNS, ids),
        (CONTROL_PATTERNS, ids),
        (CONSENT_PATTERNS, ids),
        (BILLING_PATTERNS, []),
    )
    for patterns, scope_ids in classes:
        for pattern, tier in patterns.items():
            if _RISK_ORDER[tier] <= _RISK_ORDER[risk]:
                continue
            if any(_reads(name, pattern) for name in names) or any(
                _reads_id(automation_id, pattern) for automation_id in scope_ids
            ):
                risk = tier
    return risk


def _chosen_tier(
    step: Step, bound_name: str | None, element_ids: Sequence[str], group: str | None
) -> Risk:
    """W4-03: a choose is the option it picks. Deny, control, consent and billing read the
    option, the ComboBox's name and its group's name ("Premium ($20/mo)" is R2, "Delete
    selected" R3, "Yes" under a consent group R2); the automation ids through every class
    but billing. An option's own automation id is invisible before Expand (a 5.13 gap)."""
    target = step.target
    if target is None:  # the caller checked it; kept for the type checker
        return "R0"
    option = step.params.get(OPTION_PARAM)
    names = [target.name, *(n for n in (option, bound_name, group) if n)]
    ids = [i for i in (target.automation_id, *element_ids) if i]
    return _every_class(names, ids)


def group_name(snapshot: Snapshot, element: Element) -> str | None:
    """The name of the element's nearest ancestor when that ancestor is a named Group (W3-03):
    Chrome 156 names it after a fieldset's legend or a radiogroup's aria-label (probe
    `widget_patterns --section groups`, n=3), and a ListItem's parent when that is a named
    List (#206 review: the container names its options, as a ComboBox does). A question in a
    plain div names no ancestor (a written gap); anything else is None."""
    if element.parent_id is None:
        return None
    try:
        parent = snapshot.element(element.parent_id)
    except SteerError:
        return None
    container = parent.role == GROUP_ROLE or (
        element.role == SELECTED_CONTROL_ROLE and parent.role == LISTBOX_PARENT_ROLE
    )
    if not container or not normalise_name(parent.name):
        return None
    return parent.name


def rename_counts(
    step: Step, expect: Predicate | None, *, risk: Risk, deny: Risk, goal: str
) -> bool:
    """Whether an in-place page rename is a search submit's effect (5.10 [v2.39], amending
    [v2.1]): an eligible search submit (`url_query_eligible`) whose expect is in a code-owned
    frame. A page that sets its own title after the approved Enter is the residual risk; sends,
    deletes and anything raised keep `renamed_at_risk`."""
    return (
        url_query_eligible(step, expect, risk=risk, deny=deny, goal=goal)
        and isinstance(expect, Appeared)
        and expect.decoration is not None
    )


def url_query_eligible(
    step: Step, expect: Predicate | None, *, risk: Risk, deny: Risk, goal: str
) -> bool:
    """A search submit (5.10 [v2.39]; #148 design D item 3): a `submit` at exactly R2 from its
    floor alone (plan tier at most R2, no deny word or automation id raising it), whose expect
    is the bare replacing form of a Document naming exactly the typed text, which the goal
    holds verbatim."""
    text = step.params.get("text")
    return (
        step.verb == "submit"
        and risk == "R2"
        and _RISK_ORDER[step.risk] <= _RISK_ORDER["R2"]
        and _RISK_ORDER[deny] < _RISK_ORDER["R2"]
        and isinstance(expect, Appeared)
        and expect.replaces
        and expect.role == "Document"
        and isinstance(text, str)
        and expect.name_pattern == text
        and verbatim_in(text, goal)
    )


def _reads(text: str, pattern: str) -> bool:
    """The plain words (5.13 1a [v1.3]), or the closure through lookalikes ([v2.26], #83)."""
    return _contains(_words(text), pattern) or holds(text, pattern)


def _reads_id(automation_id: str, pattern: str) -> bool:
    if _contains(_id_words(automation_id), pattern):
        return True
    # split and whole: a case change inside the word ("SUBMit") must not hide it either
    return holds(camel_split(automation_id), pattern) or holds(automation_id, pattern)


@dataclass(frozen=True, slots=True)
class Ask:
    """One question to the user: tokens for the trace, parameters for the confirmer to show."""

    decision_id: str
    task_id: str
    action_key: str
    verb: str
    target_role: str
    risk: Risk
    expect: Mapping[str, Any]
    params: Mapping[str, str] = field(repr=False)
    previous_attempt: str | None = None
    target_name: str = field(default="", repr=False)
    automation_id: str = field(default="", repr=False)
    kind: Literal["yes", "which"] = "yes"
    """`yes`: consent to one action; `which`: the identity ask, choose among candidates."""
    candidates: tuple[CandidateLine, ...] = field(default=(), repr=False)
    window: str = field(default="", repr=False)
    """Where the step lands: the app and reference by code, then the page's own title
    (step-window SW-06). Never in the trace."""
    reasons: tuple[str, ...] = ()
    """Why a step below R2 asks anyway (5.13 [v2.27]): closed tokens, e.g. `generated_text`."""


@dataclass(frozen=True, slots=True)
class WhichResult:
    outcome: Literal["chosen", "block", "pending"]
    index: int | None
    code: ErrorCode | None
    evidence: dict[str, Any]


class Confirmer(Protocol):
    """Shows the ask and returns the user's token: "yes", "no", "unanswered", or the token
    suffixed with the decision id (`yes:<id>`). Never return on a bare Enter (OR-14)."""

    def ask(self, ask: Ask) -> str: ...


@dataclass(frozen=True, slots=True)
class GateResult:
    outcome: Outcome
    decision: GateDecision | None
    code: ErrorCode | None
    evidence: dict[str, Any]
    expect: Predicate | None = None
    params: Params | None = None


def _bound_present(
    candidate: Predicate, step: Step, params: Params, risk: Risk
) -> Predicate | GateResult:
    """A `present` proves a click's selection only with the clicked element bound: its
    container never counts (#124, hunter PS-02). Anything else is blocked, never verified."""
    if not presents(candidate):
        return candidate
    if not isinstance(candidate, Present) or step.verb != "click" or params.target_id is None:
        return _block("weak_predicate", risk, reason="present_unbound")
    return replace(candidate, target_id=params.target_id)


def _block(detail: str, risk: Risk, **extra: Any) -> GateResult:
    return GateResult(
        outcome="block",
        decision=None,
        code=ErrorCode.GATE_BLOCKED,
        evidence={"risk": risk, "detail": detail, **extra},
    )


def params_for(
    step: Step,
    target_id: int | None = None,
    composer_id: int | None = None,
    bound: Binding | None = None,
) -> Params:
    """Strength parameters filled by code from the step, never from model text (5.4 [v0.5]);
    `target_id` is the element the loop resolved for a role-and-name target; for a person
    target the bound element's full name is the name the templates match (PT-09)."""
    raw = step.params
    composer = raw.get("composer_id")
    if composer is None and composer_id is not None:
        composer = str(composer_id)
    scope = raw.get("scope")
    resolved = step.target.element_id if step.target is not None else None
    return Params(
        target_id=resolved if resolved is not None else target_id,
        target_role=step.target.role if step.target is not None else None,
        target_name=(
            bound.full_name
            if bound is not None
            else step.target.name
            if step.target is not None
            else None
        ),
        text=step.text if step.text is not None else raw.get("text"),
        composer_id=int(composer) if composer is not None else None,
        composer_name=raw.get("composer_name"),
        scope=Within(landmark=scope) if scope else None,
        content_role=raw.get("content_role", "Text"),
        window_name=_open_title(step) or raw.get("window_name"),
        open_title=_open_title(step),
        file_name=raw.get("file_name"),
        recipient=raw.get("recipient"),
        option=raw.get(OPTION_PARAM) if step.verb == "choose" else None,
    )


def _open_title(step: Step) -> str | None:
    if step.verb != "open" or step.target is None:
        return None
    try:
        return expected_title(step.target.name, step.params.get("site"), step.params.get("text"))
    except LaunchError:
        return None


def open_tier(goal: str, step: Step) -> Risk:
    """An `open`'s tier from the plan's goal, by code (schema gate P1, decision 2; OW-01/02):
    content in a URL leaves the machine, so it stays R1 only when every word of it is a word
    of the goal and the goal names the site; anything else confirms at R2."""
    risk: Risk = step.risk
    site = step.params.get("site")
    text = step.params.get("text")
    if site is None or text is None:
        return risk
    goal_tokens = set(_tokens(goal))
    spec = SITES.get(site)
    named = spec is not None and bool(spec.names & goal_tokens)
    symbols = {ch for ch in comparable(text) if not (ch.isalnum() or ch.isspace())}
    goal_symbols = set(comparable(goal))
    # every word and every symbol of the content must be the goal's own (review item 1)
    if named and symbols <= goal_symbols and set(_tokens(text)) <= goal_tokens:
        return risk
    return "R2" if _RISK_ORDER[risk] < _RISK_ORDER["R2"] else risk


def _opens(step: Step) -> dict[str, str]:
    """What an `open`'s ask shows besides the content: the host the URL goes to."""
    if step.verb != "open" or step.target is None:
        return {}
    try:
        url = site_url(step.target.name, step.params.get("site"), step.params.get("text"))
    except LaunchError:
        return {}
    if url is None:
        return {}
    return {"opens": urllib.parse.urlsplit(url).hostname or ""}


class Gate:
    def __init__(self, *, confirmer: Confirmer, own_pid: int, task_id: str) -> None:
        self._confirmer = confirmer
        self._own_pid = own_pid
        self._task_id = task_id

    def predicate_for(
        self,
        step: Step,
        target_id: int | None = None,
        composer_id: int | None = None,
        bound: Binding | None = None,
    ) -> Predicate | None:
        """The step's effect as the gate would check it: its own `expect`, else the template."""
        if step.expect is not None:
            return step.expect
        try:
            return template_for(
                template_verb(step.verb), params_for(step, target_id, composer_id, bound)
            )
        except ValueError:
            return None

    def which(
        self, step: Step, elements: Sequence[Element], *, previous_attempt: str | None = None
    ) -> WhichResult:
        """The identity ask (5.8 [v1.5]): several rows name the person, so the person chooses.
        Only `<n>:<ask id>` with 1 <= n <= k counts; `no` is `none_chosen`; the kill switch
        blocks; anything else pauses. No gate decision is consumed by this ask."""
        risk = effective_risk(step)
        parameter = step.target.name if step.target is not None else ""
        lines = lines_for(elements, parameter)
        ask_id = f"w-{uuid.uuid4().hex[:12]}"
        counts = {
            "candidates": len(lines),
            "lookalikes": sum(1 for line in lines if "lookalike" in line.tags),
            "mixed_script": sum(1 for line in lines if "mixed script" in line.tags),
        }
        ask = Ask(
            decision_id=ask_id,
            task_id=self._task_id,
            action_key="",
            verb=step.verb,
            target_role=step.target.role if step.target is not None else "",
            risk=risk,
            expect={},
            params={},
            previous_attempt=previous_attempt,
            target_name=parameter,
            kind="which",
            candidates=tuple(lines),
        )
        answer = self._confirmer.ask(ask)
        chosen = _WHICH.fullmatch(answer or "")
        if chosen is not None and chosen.group(2) == ask_id:
            index = int(chosen.group(1))
            if 1 <= index <= len(lines):
                return WhichResult("chosen", index, None, {**counts, "chosen_index": index})
        if answer in ("no", f"no:{ask_id}"):
            return WhichResult(
                "block", None, ErrorCode.GATE_BLOCKED, {**counts, "detail": "none_chosen"}
            )
        if answer == "killed":
            return WhichResult(
                "block", None, ErrorCode.GATE_BLOCKED, {**counts, "detail": "killed"}
            )
        return WhichResult(
            "pending", None, ErrorCode.GATE_PENDING_USER, {**counts, "detail": "unanswered"}
        )

    def decide(  # noqa: PLR0913 - keyword-only context from the loop, each part optional
        self,
        step: Step,
        *,
        action_key: str,
        expect: Predicate | None,
        target_pid: int,
        previous_attempt: str | None = None,
        target_id: int | None = None,
        composer_id: int | None = None,
        bound: Binding | None = None,
        window: str = "",
        goal: str = "",
        verdict: str | None = None,
        element_automation_id: str = "",
        group_name: str | None = None,
    ) -> GateResult:
        risk = effective_risk(
            step,
            bound.shown_name_part if bound is not None else None,
            (element_automation_id,) if element_automation_id else (),
            group_name=group_name,
        )
        if target_pid == self._own_pid:
            return _block("self_target", risk)
        params = params_for(step, target_id, composer_id, bound)
        if step.verb == "open":
            expect = None  # the code-owned title, never the model's effect (P2)
        predicate = self._predicate(step, expect, risk, params)
        if isinstance(predicate, GateResult):
            return predicate
        decision_id = f"d-{uuid.uuid4().hex[:12]}"
        reasons = ask_reasons(step, goal, verdict)
        if risk in ("R0", "R1") and not reasons:
            decision = GateDecision(
                decision_id=decision_id, action_key=action_key, outcome="approve"
            )
            return GateResult(
                outcome="approve",
                decision=decision,
                code=None,
                evidence={"risk": risk},
                expect=predicate,
                params=params,
            )
        ask = Ask(
            decision_id=decision_id,
            task_id=self._task_id,
            action_key=action_key,
            verb=step.verb,
            target_role=step.target.role if step.target is not None else "",
            risk=risk,
            expect=redact(predicate),
            params={
                **step.params,
                **({"text": step.text} if step.text is not None else {}),
                **_opens(step),
            },
            previous_attempt=previous_attempt,
            target_name=(
                bound.full_name
                if bound is not None
                else step.target.name
                if step.target is not None
                else ""
            ),
            automation_id=step.target.automation_id if step.target is not None else "",
            window=window,
            reasons=reasons,
        )
        answer = self._confirmer.ask(ask)
        if answer in ("yes", f"yes:{decision_id}"):
            decision = GateDecision(
                decision_id=decision_id, action_key=action_key, outcome="confirm"
            )
            return GateResult(
                outcome="confirm",
                decision=decision,
                code=None,
                evidence={
                    "risk": risk,
                    "decision_id": decision_id,
                    **({"ask_reasons": list(reasons)} if reasons else {}),
                },
                expect=predicate,
                params=params,
            )
        return _declined(answer, risk, decision_id, reasons)

    def postcondition(
        self, step: Step, expect: Predicate | None, risk: Risk, params: Params
    ) -> Predicate | GateResult:
        """The step's postcondition through the same strength rung the decision used, for a
        rebuild after the bound row's text moved on (PT-09): the template or the plan's
        expect, strengthened where the rung strengthens, blocked where it blocks."""
        return self._predicate(step, expect, risk, params)

    def _predicate(
        self, step: Step, expect: Predicate | None, risk: Risk, params: Params
    ) -> Predicate | GateResult:
        """The step's postcondition after the template rung, or the block that stops it."""
        candidate = expect
        if candidate is None:
            try:
                candidate = template_for(template_verb(step.verb), params)
            except ValueError:
                return _block("no_predicate", risk)
        verdict = strength(candidate, risk, params, verb=template_verb(step.verb))
        if verdict.ok:
            return _bound_present(candidate, step, params, risk)
        strengthened = strengthen(candidate, template_verb(step.verb), risk, params)
        if strengthened is None:
            return _block("weak_predicate", risk, reason=verdict.reason)
        return strengthened
