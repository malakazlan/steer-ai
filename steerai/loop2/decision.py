"""The Decision schema the controller model answers with, and the code-side validator.

`validate` returns closed rejection tokens, never free text, so a repair prompt can carry them
without echoing anything the model or the screen said.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

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

UNTARGETED = frozenset({"press", "wait"})
NOT_IN_STEP_VERBS = frozenset({"open", "switch"})
MAX_ACTIONS = 4
MAX_SUBGOALS = 5
MAX_DESC_CHARS = 120
SUBGOAL_ID = re.compile(r"[a-z0-9_]{1,16}")
PARAM_NAME = re.compile(r"[a-z_]{1,32}")
CHECKED_KINDS = frozenset({"state", "value", "navigated"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SubgoalDoc(_Strict):
    id: str
    desc: str
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
) -> list[str]:
    if action.verb in NOT_IN_STEP_VERBS:
        return ["not_in_step"]
    tokens: list[str] = []
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


def _type_tokens(action: ActionDoc, params: set[str]) -> list[str]:
    has_text = bool(action.text)
    if has_text and action.param is not None:
        return ["type_text_and_param"]
    if not has_text and action.param is None:
        return ["text_missing"]
    if action.param is not None and action.param not in params:
        return ["param_unknown"]
    return []


def _claim_tokens(
    claim: Claim, listed_ids: frozenset[int], known: Mapping[str, set[str]]
) -> list[str]:
    tokens: list[str] = []
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


def _subgoal_doc_tokens(sub: SubgoalDoc, goal: str, fences: frozenset[str]) -> list[str]:
    tokens: list[str] = []
    if SUBGOAL_ID.fullmatch(sub.id) is None:
        tokens.append("subgoal_id_bad")
    bad = has_control(sub.desc) or any(fence in sub.desc for fence in fences)
    if len(sub.desc) > MAX_DESC_CHARS or bad:
        tokens.append("desc_bad")
    if any(PARAM_NAME.fullmatch(name) is None for name in sub.params):
        tokens.append("param_name_bad")
    if any(not value or value not in goal for value in sub.params.values()):
        tokens.append("param_not_span")
    return tokens


def _subgoal_tokens(
    decision: Decision,
    goal: str,
    first_turn: bool,
    known: dict[str, set[str]],
    fences: frozenset[str],
) -> list[str]:
    subs = decision.subgoals
    if subs is None or (first_turn and not subs):
        return ["subgoals_missing"] if first_turn else []
    if not first_turn:
        return ["subgoals_after_first"]
    tokens: list[str] = []
    if len(subs) > MAX_SUBGOALS:
        tokens.append("too_many_subgoals")
    if len({sub.id for sub in subs}) != len(subs):
        tokens.append("subgoal_duplicate")
    for sub in subs:
        known.setdefault(sub.id, set()).update(sub.params)
        tokens.extend(_subgoal_doc_tokens(sub, goal, fences))
    return tokens


def _shape_tokens(decision: Decision) -> list[str]:
    tokens: list[str] = []
    if not (decision.claims or decision.actions) and decision.ask is None and decision.stop is None:
        tokens.append("empty_decision")
    if decision.ask is not None and not clean_line(decision.ask):
        tokens.append("ask_empty")
    if decision.actions and decision.ask is not None:
        tokens.append("ask_with_actions")
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
) -> list[str]:
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
