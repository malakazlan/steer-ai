"""The Decision schema the controller model answers with, and the code-side validator.

`validate` returns closed rejection tokens, never free text, so a repair prompt can carry them
without echoing anything the model or the screen said.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from steerai.loop2.task import Commit

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
    target: int | None = None
    subgoal: str
    text: str | None = None
    option: str | None = None
    key: Key | None = None
    param: str | None = None
    commit: Commit = "none"


class Claim(_Strict):
    subgoal: str
    kind: ClaimKind
    evidence: list[int] = Field(default_factory=list)
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
    action: ActionDoc, listed_ids: frozenset[int], params: frozenset[str]
) -> list[str]:
    if action.verb in NOT_IN_STEP_VERBS:
        return ["not_in_step"]
    tokens: list[str] = []
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
        if action.text is not None and action.param is not None:
            tokens.append("type_text_and_param")
        elif action.text is None and action.param is None:
            tokens.append("text_missing")
        elif action.param is not None and action.param not in params:
            tokens.append("param_unknown")
    return tokens


def _subgoal_tokens(
    decision: Decision, goal: str, first_turn: bool, known: dict[str, set[str]]
) -> list[str]:
    tokens: list[str] = []
    if decision.subgoals is None:
        if first_turn:
            tokens.append("subgoals_missing")
        return tokens
    if not first_turn:
        return ["subgoals_after_first"]
    if len(decision.subgoals) > MAX_SUBGOALS:
        tokens.append("too_many_subgoals")
    for sub in decision.subgoals:
        known.setdefault(sub.id, set()).update(sub.params)
        if any(value not in goal for value in sub.params.values()):
            tokens.append("param_not_span")
    return tokens


def validate(
    decision: Decision,
    *,
    listed_ids: frozenset[int],
    goal: str,
    first_turn: bool,
    subgoal_params: Mapping[str, frozenset[str]] | None = None,
) -> list[str]:
    """Return the closed rejection tokens for a decision, deduplicated, in first-seen order.

    `subgoal_params` maps each known subgoal id to the names of its params; subgoals declared by
    this decision (first turn) are added to it.
    """
    known = {sid: set(names) for sid, names in (subgoal_params or {}).items()}
    tokens = _subgoal_tokens(decision, goal, first_turn, known)
    if not (decision.claims or decision.actions) and decision.ask is None and decision.stop is None:
        tokens.append("empty_decision")
    if decision.actions and decision.ask is not None:
        tokens.append("ask_with_actions")
    if decision.actions and decision.stop is not None:
        tokens.append("stop_with_actions")
    if len(decision.actions) > MAX_ACTIONS:
        tokens.append("too_many_actions")
    for action in decision.actions:
        tokens.extend(
            _action_tokens(action, listed_ids, frozenset(known.get(action.subgoal, set())))
        )
    for claim in decision.claims:
        if claim.kind not in CHECKED_KINDS:
            tokens.append("not_in_step")
        if any(ev not in listed_ids for ev in claim.evidence):
            tokens.append("id_not_listed")
    return list(dict.fromkeys(tokens))
