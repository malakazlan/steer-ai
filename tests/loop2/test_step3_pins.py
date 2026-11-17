"""[V2.86] (4): step 3's wording and packet builders are frozen while step 4 builds beside them.

Step 3's counted runs use a frozen commit whatever main does. With the step 4 flag off, every
value pinned in this file stays byte-identical: a PR that changes one is rejected unless the lead
rules otherwise. Step 4 must not edit these constants or what these builders render; it adds keyed
variants beside them (`SYSTEM_RULES_S4`, a step 4 schema, `REPAIR_LEGEND_S4`, `INTAKE_RULES_S4`,
`AUDIT_RULES_S4`, ...), chosen by the flag and pinned in a file of their own.

This file is the single place that states "step 3 frozen". Values were computed at origin/main
84ce86c (controller-15, intake-5, audit-6). Older pins elsewhere stay in force and are not
removed: the judges' system blocks (test_prompt_cache.py), the intake and audit fixed blocks with
their versions (test_intake_check.py) and `chain_eval.audit_pins()` (test_chain_eval.py) are
restated here; v1's `action_key` on fixed inputs is pinned only in test_ledger_keys.py ([V2.15]
Q1) and is frozen by this ruling there, not duplicated here.
"""

from __future__ import annotations

import hashlib
import json

from steerai import goal_match
from steerai.loop2 import chain_eval, controller
from steerai.loop2.chain_pages import Page, PageWorld
from steerai.loop2.controller import ListingRow, build_intake_prompt, build_prompt
from steerai.loop2.decide_port import DecideCalls, Reply, TaskGenerations
from steerai.loop2.history import LogEntry, Vocab
from steerai.loop2.loop import IntakeLine, TurnView
from steerai.loop2.prompt_inputs import make_decide
from steerai.loop2.task import GoalSpan, Subgoal
from tests.loop2.test_prompt_cache import FENCE, SCREEN

GOAL = "type Ada Lovelace as the full name and keep Kenya selected"
LOG = (
    LogEntry(
        verb="click",
        handle=1,
        role="edit",
        subgoal="s1",
        param=None,
        delivery="delivered_verified",
        classes=("focus",),
        tokens=(),
        read_back=None,
    ),
    LogEntry(
        verb="type",
        handle=1,
        role="edit",
        subgoal="s1",
        param="text",
        delivery="delivered_verified",
        classes=("own_value",),
        tokens=(),
        read_back=None,
    ),
)
ROWS = (
    ListingRow(0, '- document "Profile"'),
    ListingRow(1, '1 edit "Full name" value="Ada Lovelace" focused', handle=1),
    ListingRow(1, '2 combobox "Country" collapsed value="Kenya"', handle=2),
)
SUBGOAL_LINES = (
    's1 closes=value commit=none: the name holds it; text="Ada Lovelace"',
    's2 closes=state commit=none: Kenya stays selected; what="Kenya"',
)
INTAKE_LINES: tuple[IntakeLine, ...] = (
    (1, "value", ((1, "Ada Lovelace"),), None, "full name"),
    (2, "state", ((1, "Kenya"),), "selected", None),
    (3, "page", ((1, "Ada Lovelace"),), None, None),
)
PAGE = {
    "title": "Profile",
    "controls": [
        {"kind": "field", "name": "Full name"},
        {"kind": "select", "name": "Country", "options": ["Kenya", "Ghana", "Peru"]},
        {"kind": "button", "name": "Save"},
    ],
}
SUBGOALS = (
    Subgoal(id="s1", desc="the name holds it", params={"text": GoalSpan.of(GOAL, 5, 17)},
            closes="value", field="full name"),
    Subgoal(id="s2", desc="Kenya stays selected", params={"what": GoalSpan.of(GOAL, 44, 49)},
            closes="state", wanted="selected"),
)  # fmt: skip


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _blocks(blocks: list[str]) -> str:
    """One digest of a list of blocks; JSON keeps the boundaries between them."""
    return _sha(json.dumps(blocks, ensure_ascii=False))


# ---- the decide wording --------------------------------------------------------------------------

# Guards the decide system rules, step 3; changing it needs a lead ruling ([V2.86] (4)).
SYSTEM_RULES = "6768a0b976c1066a430749fafeb39966e45a3422563343723c699b470ef69c1a"
# Guards the first turn's Decision schema as the prompt shows it; lead ruling ([V2.86] (4)).
SCHEMA_FIRST = "45d33f68f06d7673a75a167bcecf7edfff54253f234d286109bfa96267ae0345"
# Guards a later turn's Decision schema as the prompt shows it; lead ruling ([V2.86] (4)).
SCHEMA_LATER = "c690da9591b6a17390c491bd30f3ef9547b56c7b431e11d8e5d3b5948f68c5a7"
# Guards the repair legend, its tokens, meanings and order; lead ruling ([V2.86] (4)).
REPAIR_LEGEND = "beccde05fb42ac081f43acc3a9ec1a7f56f8dd5169d2b070ef58135174bd7be2"
# Guards the prompt versions the frozen wording is recorded under; lead ruling ([V2.86] (4)).
VERSIONS = ("controller-15", "intake-5", "audit-6", "goal-match-2")


def test_the_decide_system_rules_are_frozen() -> None:
    assert _sha(controller.SYSTEM_RULES) == SYSTEM_RULES


def test_the_decide_schema_of_the_first_and_of_a_later_turn_are_frozen() -> None:
    first = json.dumps(controller._prompt_schema(first_turn=True), sort_keys=True)
    later = json.dumps(controller._prompt_schema(first_turn=False), sort_keys=True)
    assert (_sha(first), _sha(later)) == (SCHEMA_FIRST, SCHEMA_LATER)


def test_the_repair_legend_is_frozen() -> None:
    assert _sha(json.dumps(list(controller.REPAIR_LEGEND.items()))) == REPAIR_LEGEND


def test_the_prompt_versions_are_frozen() -> None:
    versions = (
        controller.PROMPT_VERSION,
        controller.INTAKE_PROMPT_VERSION,
        controller.AUDIT_PROMPT_VERSION,
        goal_match.GOAL_MATCH_PROMPT_VERSION,
    )
    assert versions == VERSIONS


# ---- the decide packet ---------------------------------------------------------------------------

# Guards build_prompt's blocks on a first turn, fixed input and fences; lead ruling ([V2.86] (4)).
BUILD_FIRST = (
    "bb25854cf25a6c16606730445b3efc78caea885927ad1e773d87df795d18e141",
    "09b6718ee8e98fbbe40848ddcf8a5d9c3681bfbac72da9afa2db7f89b6c0f16f",
)
# Guards build_prompt's blocks on a later turn with every header line; lead ruling ([V2.86] (4)).
BUILD_LATER = (
    "60486a5eebc8498dc5e3e089bb1c735682fa26d10453060701791e572852dd01",
    "0dc0c88c00d5421bb57fd86efc0a25a676260529f80451904b31c14258e74d63",
)
# Guards make_decide's whole request on a fixed page, fences masked; lead ruling ([V2.86] (4)).
PACKET = (
    "e50fc76fc51d0528bad3990da750b93d444900bd17d028c640c7e7d1240ef9b6",
    "37b468a28c85956cba3cabcabb1e5bcf4dd7b9acd7419c836fd2895b81395ccd",
)


def test_build_prompt_on_a_fixed_first_turn_is_frozen() -> None:
    system, user = build_prompt(GOAL, [], [], [], "Profile", list(ROWS),
                                task_fence=FENCE, screen_fence=SCREEN)  # fmt: skip
    assert (_blocks(system), _blocks(user)) == BUILD_FIRST


def test_build_prompt_on_a_fixed_later_turn_is_frozen() -> None:
    system, user = build_prompt(
        GOAL,
        list(SUBGOAL_LINES),
        list(LOG),
        [("s1", "done"), ("s2", "open")],
        "Profile",
        list(ROWS),
        task_fence=FENCE,
        screen_fence=SCREEN,
        unstable=True,
        growing=True,
        young=True,
        vocab=Vocab({"s1": frozenset({"text"}), "s2": frozenset({"what"})}, frozenset({1, 2})),
        focus="elsewhere",
        listing_note="listing: 3 of 9 rows",
        changes_note="changes: states unread",
    )
    assert (_blocks(system), _blocks(user)) == BUILD_LATER


def _packet(view: TurnView) -> str:
    """The first request make_decide sends for the view, every fence token masked."""
    sent: list[list[str]] = []

    def provider(system: list[str], blocks: list[str]) -> Reply:
        sent.append([*system, *blocks])
        return Reply('{"stop": "blocked"}', "end_turn", None)

    calls = DecideCalls(
        provider,
        interrupted=lambda: None,
        generations=TaskGenerations(),
        late_error=lambda _token: None,
    )
    make_decide(calls, goal=GOAL, task_fence="pinfence01")(view)
    return chain_eval.masked_sha256(sent[0])


def test_the_decide_packet_on_a_fixed_page_is_frozen() -> None:
    """The packet builder: the listing's selection and rows, the subgoal lines, the header notes
    and where the fences sit, as make_decide renders them on a first and on a later turn."""
    seen = PageWorld(Page.from_spec(PAGE)).observe()
    handles = {e.id: e.id for e in seen.snapshot.elements}
    first = TurnView(observed=seen, log=(), first_turn=True, handles=handles)
    later = TurnView(observed=seen, log=LOG, first_turn=False, handles=handles,
                     subgoals=SUBGOALS, proven=frozenset({"s1"}), issued=frozenset({1}),
                     changes="states_unread")  # fmt: skip
    assert (_packet(first), _packet(later)) == PACKET


# ---- the judges ----------------------------------------------------------------------------------

# Guards the intake prompt on fixed declared lines and fence; lead ruling ([V2.86] (4)).
INTAKE = (
    "ff20e5f3104f2330b727f0a18e60f9e7a452d7a32afd388cde3ddffacc6adec0",
    "6ad852549092089a07fc5ce3a96d50d3cbcce0fd92432d6193e39ff121bdb201",
)
# Guards chain_eval.audit_pins(): the audit's prompt and packet; lead ruling ([V2.86] (4)).
AUDIT = {
    "prompt": "bf644107c14a7203e5294e58a2cc3d24faef8a7b6250837f2a5189cb983471de",
    "packet": "6de6da5e293f3872216382d4b3aa62afb937c903832178bd4be1c0460a1fd109",
}
# Guards v1's goal match system prompt (goal-match-2); lead ruling ([V2.86] (4)).
GOAL_MATCH = "04c4eb7d8905fc82126a2d8ec9c9f7de837ffb1ff9daa0d5f130e72a5bd69f5e"


def test_the_intake_prompt_on_fixed_declared_lines_is_frozen() -> None:
    system, user = build_intake_prompt(GOAL, INTAKE_LINES, task_fence=FENCE)
    assert (_blocks(system), _blocks(user)) == INTAKE


def test_the_audit_prompt_and_packet_are_frozen() -> None:
    assert chain_eval.audit_pins() == AUDIT


def test_the_v1_goal_match_system_prompt_is_frozen() -> None:
    assert _sha(goal_match._SYSTEM) == GOAL_MATCH
