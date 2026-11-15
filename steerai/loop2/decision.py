"""The Decision schema the controller model answers with, and the code-side validator.

`validate` returns closed rejection tokens, never free text, so a repair prompt can carry them
without echoing anything the model or the screen said.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from steerai.loop2.task import Commit
from steerai.loop2.text import clean_line, has_control

Verb = Literal[
    "click",
    "type",
    "press",
    "check",
    "uncheck",
    "select",
    "expand",
    "collapse",
    "choose",
    "scroll",
    "focus",
    "open",
    "switch",
    "wait",
]
Key = Literal[
    "Enter",
    "Escape",
    "Tab",
    "Up",
    "Down",
    "Left",
    "Right",
    "PageUp",
    "PageDown",
    "Home",
    "End",
    "Space",
    "Backspace",
]
ClaimKind = Literal[
    "state", "value", "navigated", "content_present", "content_gone", "window", "file", "answer"
]

Closes = Literal["value", "state", "page"]
"""[V2.39]: value (a field holds the text), state (an element is in a state), page (the page
shows it: navigated, and content_present from step 4)."""
CLAIM_CLOSES: Mapping[str, Closes] = {
    "value": "value",
    "state": "state",
    "navigated": "page",
    "content_present": "page",
}
"""[V2.39]: the closing kind each claim kind proves; a claim of another kind is `kind_mismatch`."""
ValidateToken = Literal[
    "ask_empty",
    "ask_with_actions",
    "ask_with_stop",
    "commit_cannot_apply",
    "desc_bad",
    "empty_decision",
    "id_not_listed",
    "key_missing",
    "not_in_step",
    "option_missing",
    "param_missing",
    "param_name_bad",
    "param_not_span",
    "param_unknown",
    "stop_with_actions",
    "subgoal_duplicate",
    "subgoal_id_bad",
    "subgoal_unknown",
    "subgoals_after_first",
    "subgoals_missing",
    "target_missing",
    "target_on_untargeted_verb",
    "text_bad",
    "text_missing",
    "too_many_actions",
    "too_many_subgoals",
    "type_text_and_param",
    "wanted_missing",
    "value_without_param",
    "state_without_param",
    "page_without_param",
]
"""#349: every refusal `validate` can give (the model's shape faults), closed for mypy."""
WITHOUT_PARAM: Final[dict[Closes, ValidateToken]] = {
    "value": "value_without_param",
    "state": "state_without_param",
    "page": "page_without_param",
}
"""[V2.39], [V2.40] Q7: the refusal for a subgoal with no param, one literal per closing kind
(#347 review: no token is built from runtime data)."""

UNTARGETED = frozenset({"press", "wait"})
NO_COMMIT_VERBS = frozenset({"wait", "scroll", "focus"})
"""[V2.42] RT-42: verbs that cannot commit anything; a declared commit on one is a shape error
(a model fault). Every other verb: a declared commit still raises the tier."""
STEP: Final = 3
"""The v2 loop's current step (V2 §12)."""
STEP_VERBS: Final[Mapping[int, frozenset[str]]] = {
    3: frozenset(get_args(Verb)) - {"open", "switch", "scroll"},
}
"""[V2.44] (1): the one table, keyed by step, of the verbs a Decision may use. The prompt offers
exactly these and `validate` accepts exactly these. Step 3 leaves out `open` and `switch`
(window switching is step 7) and `scroll` ([V2.41] (2), #299: no stage 1 row needs it, and
`ScrollPattern.Scroll()` is unprobed, a step 5 probe task)."""
NOT_IN_STEP_VERBS: Final = frozenset(get_args(Verb)) - STEP_VERBS[STEP]
"""Refused as a step limit (`not_in_step`): the backstop for a model that sends one anyway."""
MAX_ACTIONS = 4
MAX_SUBGOALS = 5
MAX_DESC_CHARS = 120
SUBGOAL_ID_CHARS = 16
PARAM_NAME_CHARS = 24
SUBGOAL_ID = re.compile(r"[a-z0-9_]{1,16}")
PARAM_NAME = re.compile(r"[a-z0-9_]{1,24}")
"""Literals (a token producer builds no string at runtime); a test pins each bound to its
`*_CHARS` constant, which the prompt's schema states."""
"""[V2.47]: a param name is closed at intake: lowercase letters, digits and underscore, at most
24 characters (subgoal ids: the same alphabet, at most 16, `SUBGOAL_ID`)."""
STEP_KINDS: Final[Mapping[int, frozenset[str]]] = {
    3: frozenset({"state", "value", "navigated"}),
}
"""#386, [V2.44] (1)'s rule for verbs applied to claim kinds: the one table, keyed by step, of the
kinds a Decision may claim. The prompt offers exactly these and `validate` accepts exactly
these; step 3 checks state, value and navigated (the other kinds are steps 4 to 7)."""
CHECKED_KINDS = STEP_KINDS[STEP]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SubgoalDoc(_Strict):
    id: str
    desc: str
    closes: Closes
    """[V2.39] the kind of fact that closes it, declared at intake and frozen with it."""
    params: dict[str, str] = Field(default_factory=dict)
    commit: Commit = "none"


class ActionDoc(_Strict):
    verb: Verb
    target: StrictInt | None = None
    subgoal: str
    text: str | None = None
    option: str | None = None
    key: Key | None = None
    param: str | None = None
    commit: Commit = "none"


class Claim(_Strict):
    subgoal: str
    kind: ClaimKind
    evidence: list[StrictInt] = Field(default_factory=list)
    param: str | None = None
    wanted: Literal["on", "off", "selected", "expanded", "collapsed"] | None = None
    answer: str | None = None


class Decision(_Strict):
    subgoals: list[SubgoalDoc] | None = None
    claims: list[Claim] = Field(default_factory=list)
    actions: list[ActionDoc] = Field(default_factory=list)
    ask: str | None = None
    stop: Literal["cannot", "blocked"] | None = None


def _action_tokens(
    action: ActionDoc, listed_ids: frozenset[int], known: Mapping[str, set[str]]
) -> list[ValidateToken]:
    tokens: list[ValidateToken] = []
    if action.commit != "none" and action.verb in NO_COMMIT_VERBS:
        tokens.append("commit_cannot_apply")  # [V2.42] RT-42: these verbs cannot commit
    if action.verb in NOT_IN_STEP_VERBS:
        return [*tokens, "not_in_step"]
    if action.subgoal not in known:
        tokens.append("subgoal_unknown")
    if action.verb in UNTARGETED:
        if action.target is not None:
            tokens.append("target_on_untargeted_verb")
    elif action.target is None:
        tokens.append("target_missing")
    elif action.target not in listed_ids:
        tokens.append("id_not_listed")
    if action.verb == "press" and action.key is None:
        tokens.append("key_missing")
    if action.verb == "choose" and action.option is None:
        tokens.append("option_missing")
    if action.verb == "type":
        tokens.extend(_type_tokens(action, known.get(action.subgoal, set())))
    return tokens


def _type_tokens(action: ActionDoc, params: set[str]) -> list[ValidateToken]:
    has_text = bool(action.text)
    if action.text is not None and has_control(action.text):
        return ["text_bad"]  # RT-38: free text is one line of printable text
    if has_text and action.param is not None:
        return ["type_text_and_param"]
    if not has_text and action.param is None:
        return ["text_missing"]
    if action.param is not None and action.param not in params:
        return ["param_unknown"]
    return []


def _claim_tokens(
    claim: Claim, listed_ids: frozenset[int], known: Mapping[str, set[str]]
) -> list[ValidateToken]:
    tokens: list[ValidateToken] = []
    if claim.kind not in CHECKED_KINDS:
        tokens.append("not_in_step")
    if claim.subgoal not in known:
        tokens.append("subgoal_unknown")
    if any(ev not in listed_ids for ev in claim.evidence):
        tokens.append("id_not_listed")
    if claim.param is not None and claim.param not in known.get(claim.subgoal, set()):
        tokens.append("param_unknown")
    if claim.kind == "value" and claim.param is None:
        tokens.append("param_missing")
    if claim.kind == "state" and claim.wanted is None:
        tokens.append("wanted_missing")
    return tokens


def _subgoal_doc_tokens(sub: SubgoalDoc, goal: str, fences: frozenset[str]) -> list[ValidateToken]:
    tokens: list[ValidateToken] = []
    if SUBGOAL_ID.fullmatch(sub.id) is None:
        tokens.append("subgoal_id_bad")
    bad = has_control(sub.desc) or any(fence in sub.desc for fence in fences)
    if len(sub.desc) > MAX_DESC_CHARS or bad:
        tokens.append("desc_bad")
    if any(PARAM_NAME.fullmatch(name) is None for name in sub.params):
        tokens.append("param_name_bad")
    if any(not value or value not in goal for value in sub.params.values()):
        tokens.append("param_not_span")
    if not sub.params:
        # [V2.39], [V2.40] Q7: every closing kind proves params (page_without_param,
        # value_without_param, state_without_param); with none it could never close
        tokens.append(WITHOUT_PARAM[sub.closes])
    return tokens


def _subgoal_tokens(
    decision: Decision,
    goal: str,
    first_turn: bool,
    known: dict[str, set[str]],
    fences: frozenset[str],
) -> list[ValidateToken]:
    subs = decision.subgoals
    if subs is None or (first_turn and not subs):
        acts = bool(decision.actions or decision.claims)  # V2 §5: a first ask or stop needs none
        return ["subgoals_missing"] if first_turn and acts else []
    if not first_turn:
        return ["subgoals_after_first"]
    tokens: list[ValidateToken] = []
    if len(subs) > MAX_SUBGOALS:
        tokens.append("too_many_subgoals")
    if len({sub.id for sub in subs}) != len(subs):
        tokens.append("subgoal_duplicate")
    for sub in subs:
        known.setdefault(sub.id, set()).update(sub.params)
        tokens.extend(_subgoal_doc_tokens(sub, goal, fences))
    return tokens


def subgoal_tokens(
    decision: Decision, *, goal: str, fences: frozenset[str] = frozenset()
) -> list[ValidateToken]:
    """[V2.11] decision 1: the rejection tokens of a first decision's subgoals alone, so code can
    take valid subgoals even when an action or a claim of the same decision is refused."""
    return _subgoal_tokens(decision, goal, True, {}, fences)


def _shape_tokens(decision: Decision) -> list[ValidateToken]:
    tokens: list[ValidateToken] = []
    if not (decision.claims or decision.actions) and decision.ask is None and decision.stop is None:
        tokens.append("empty_decision")
    if decision.ask is not None and not clean_line(decision.ask):
        tokens.append("ask_empty")
    if decision.actions and decision.ask is not None:
        tokens.append("ask_with_actions")
    if decision.ask is not None and decision.stop is not None:
        tokens.append("ask_with_stop")  # RT-43: one ending at a time
    if decision.actions and decision.stop is not None:
        tokens.append("stop_with_actions")
    if len(decision.actions) > MAX_ACTIONS:
        tokens.append("too_many_actions")
    return tokens


def validate(  # noqa: PLR0913 - the decision and its keyword-only context
    decision: Decision,
    *,
    listed_ids: frozenset[int],
    goal: str,
    first_turn: bool,
    known_subgoals: Mapping[str, frozenset[str]] | None = None,
    fences: frozenset[str] = frozenset(),
) -> list[ValidateToken]:
    """Return the closed rejection tokens for a decision, deduplicated, in first-seen order.

    `known_subgoals` maps each declared subgoal id to the names of its params (build it with
    `task.known_params`); subgoals declared by this decision (first turn) are added to it. A
    subgoal desc holding any of `fences` is `desc_bad`: it is shown inside the task fence later.
    """
    known = {sid: set(names) for sid, names in (known_subgoals or {}).items()}
    tokens = _subgoal_tokens(decision, goal, first_turn, known, fences)
    tokens.extend(_shape_tokens(decision))
    for action in decision.actions:
        tokens.extend(_action_tokens(action, listed_ids, known))
    for claim in decision.claims:
        tokens.extend(_claim_tokens(claim, listed_ids, known))
    return list(dict.fromkeys(tokens))
