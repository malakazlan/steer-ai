"""Stage 1's expected outcomes (packet v2-ports-adapter, Stage 1 plan rev 4.3; coordinator ruling
2026-11-15), written before any v2 run. The tier comes from the gate's own rating
(`loop.action_risk`) on the page's saved read; the rule is three-way: R2 or above is `blocked` /
`commit_not_in_step`, else `done` when a step 3 proof kind applies, else never done."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

import pytest

from steerai.loop2.decision import Claim
from steerai.loop2.stage1_expected import (
    COMMIT_REASON,
    GALLERY_ROWS,
    LARGE_TASKS,
    Act,
    ProofKind,
    Row,
    expected_for,
    gallery_expected,
    large_expected,
    outcome_of,
    rate,
    table,
)
from steerai.loop2.task import GoalSpan, Subgoal
from steerai.loop2.verify import Readers, check_claim
from steerai.orchestrator.contracts import Risk
from steerai.probes.gallery_page import LARGE_TARGET
from steerai.probes.task_suite import GALLERY
from steerai.world.effects import Observed, ValueFact
from steerai.world.elements import Element
from steerai.world.landmarks import Landmark
from steerai.world.read_json import observed_from_json
from tests.loop2.world_fakes import element, observed

READ = Path(__file__).resolve().parents[1] / "world" / "fixtures" / "gallery" / "page_read.json"
GOAL = "search the notes for lamps"

DOC = element(1, "Document", "Page", (7, 1), None)
SHARE = element(2, "Button", "Share", (7, 2), 1)
SAVE = element(3, "Button", "Save draft", (7, 3), 1)
FORM = element(4, "Group", "", (7, 4), 1)
BOX = element(5, "Edit", "Find notes", (7, 5), 4)
NOTE = element(6, "Edit", "Note", (7, 6), 1)
ROW = element(7, "DataItem", "Order 7", (7, 7), 1)
CELL = element(8, "DataItem", "Order 7", (7, 8), 7)
TWIN_A = element(9, "Button", "Twin", (7, 9), 1)
TWIN_B = element(10, "Button", "Twin", (7, 10), 1)
SHOWN = [DOC, SHARE, SAVE, FORM, BOX, NOTE, ROW, CELL, TWIN_A, TWIN_B]


def page(*, searchbox: bool = True) -> Observed:
    roles = {BOX.runtime_id: "searchbox"} if searchbox else {}
    marks: dict[tuple[int, ...], Landmark | None] = {e.runtime_id: None for e in SHOWN}
    return dataclasses.replace(observed(SHOWN), aria_roles=roles, landmarks=marks)


TYPED_THEN_ENTER = (
    Act("type", "Edit", "Find notes", param="words"),
    Act("press", "", "", key="Enter"),
)


# ---- the three-way rule ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("risk", "proof", "outcome", "reason"),
    [
        ("R3", "state", "blocked", COMMIT_REASON),
        ("R2", "navigated", "blocked", COMMIT_REASON),
        ("R2", None, "blocked", COMMIT_REASON),
        ("R1", "value", "done", None),
        ("R0", "state", "done", None),
        ("R1", None, "never_done", None),
        ("R0", None, "never_done", None),
    ],
)
def test_the_rule_is_three_way(
    risk: Risk, proof: ProofKind | None, outcome: str, reason: str | None
) -> None:
    assert outcome_of(risk, proof) == (outcome, reason)


def test_the_commit_reason_is_the_runners_own_token() -> None:
    source = Path(__file__).resolve().parents[2] / "steerai" / "loop2" / "runner.py"
    assert f'raise _End("blocked", Reason("{COMMIT_REASON}"))' in source.read_text("utf-8")


# ---- the rating: the gate's own `action_risk`, on the read ------------------------------------


def test_a_deny_word_on_the_target_rates_r2() -> None:
    assert rate(GOAL, (Act("expand", "Button", "Share"),), page()) == "R2"


def test_a_plain_click_rates_r0() -> None:
    assert rate(GOAL, (Act("click", "Button", "Save draft"),), page()) == "R0"


def test_rating_stops_at_the_first_act_at_r2_so_a_hidden_item_after_it_is_never_needed() -> None:
    acts = (Act("expand", "Button", "Share"), Act("click", "MenuItem", "Not shown yet"))
    assert rate(GOAL, acts, page()) == "R2"


def test_an_act_whose_target_is_not_in_the_read_is_an_error_never_a_guess() -> None:
    acts = (Act("click", "Button", "Save draft"), Act("click", "MenuItem", "Not shown yet"))
    with pytest.raises(ValueError, match="not_in_read"):
        rate(GOAL, acts, page())


def test_an_ambiguous_target_is_an_error() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        rate(GOAL, (Act("click", "Button", "Twin"),), page())


def test_a_row_and_its_same_named_cell_resolve_to_the_row() -> None:
    """v1's `outermost`: a grid row and its cell carry one name; the row is the target."""
    assert rate(GOAL, (Act("select", "DataItem", "Order 7"),), page()) == "R1"


def test_an_enter_on_a_searchbox_holding_goal_words_is_a_query() -> None:
    assert rate(GOAL, TYPED_THEN_ENTER, page()) == "R1"


def test_an_enter_on_a_field_that_is_not_a_search_field_is_a_commit() -> None:
    assert rate(GOAL, TYPED_THEN_ENTER, page(searchbox=False)) == "R2"


def test_an_enter_with_no_typed_field_before_it_is_an_error() -> None:
    with pytest.raises(ValueError, match="no_acted"):
        rate(GOAL, (Act("press", "", "", key="Enter"),), page())


def test_a_goal_span_typed_into_a_plain_field_rates_r0() -> None:
    assert rate(GOAL, (Act("type", "Edit", "Note", param="words"),), page()) == "R0"


def test_expected_for_a_row_applies_the_rule_to_its_rating() -> None:
    row = Row((Act("expand", "Button", "Share"),), "state", "a menu state")
    blocked = expected_for("x1", row, GOAL, page())
    assert (blocked.outcome, blocked.reason, blocked.risk) == ("blocked", COMMIT_REASON, "R2")
    quiet = Row((Act("click", "Button", "Save draft"),), None, "only text")
    never = expected_for("x2", quiet, GOAL, page())
    assert (never.outcome, never.reason, never.risk) == ("never_done", None, "R0")


# ---- the data ---------------------------------------------------------------------------------


def test_every_gallery_goal_row_has_its_acts_and_a_one_line_reason() -> None:
    assert list(GALLERY_ROWS) == [t.task_id for t in GALLERY]
    assert len(GALLERY_ROWS) == 24
    for task_id, row in GALLERY_ROWS.items():
        assert row.acts, task_id
        assert row.why, task_id
        assert "\n" not in row.why, task_id
        assert len(row.why) <= 80, task_id


def test_the_large_tasks_are_written_as_data_and_expected_done() -> None:
    assert [(t.task_id, t.goal, t.path) for t in LARGE_TASKS] == [
        ("lg1", f"reserve item {LARGE_TARGET}", "/large.html"),
        ("lg2", "sort by size", "/large.html"),
    ]
    expected = large_expected()
    assert {k: (v.outcome, v.proof) for k, v in expected.items()} == {
        "lg1": ("done", "state"),
        "lg2": ("done", "state"),
    }


# ---- g12 to g14: done is backed by the select's own live value, not the observer's own_value --

CHOSEN = ("g12", "g13", "g14")


def _value_claim(
    task_id: str, read_value: Callable[[Element], ValueFact | None]
) -> tuple[str, ...]:
    """A `value` claim citing the row's ComboBox, checked by the runner's `check_claim` on
    observations that carry no observer value (`acted_value` None: no own_value at all)."""
    act = GALLERY_ROWS[task_id].acts[0]
    assert act.option is not None
    goal = f"pick {act.option}"
    box = element(3, act.role, act.name, (7, 3), 1)
    seen = observed([DOC, box])
    assert seen.acted_value is None
    params = {"choice": GoalSpan.of(goal, 5, len(goal))}
    subgoal = Subgoal("s1", "pick it", params, "value")
    claim = Claim(subgoal="s1", kind="value", evidence=[box.id], param="choice")

    def fact_of(text: str) -> ValueFact:
        return ValueFact(filled=bool(text), digest=f"d:{text}")

    readers = Readers(read_state=lambda _e, _w: None, read_value=read_value, fact_of=fact_of)
    return check_claim(claim, subgoal, seen, seen, readers=readers)


@pytest.mark.parametrize("task_id", CHOSEN)
def test_a_chosen_option_is_done_by_a_value_proof_on_the_combobox_itself(task_id: str) -> None:
    row = GALLERY_ROWS[task_id]
    assert row.proof == "value"
    assert [(a.verb, a.role) for a in row.acts] == [("choose", "ComboBox")]
    option = row.acts[0].option
    assert check_rejects(task_id, ValueFact(filled=True, digest=f"d:{option}")) == ()


@pytest.mark.parametrize("task_id", CHOSEN)
def test_the_value_proof_reads_the_live_value_so_an_unread_one_never_proves(task_id: str) -> None:
    """The proof is the cited element's live value read (`Readers.read_value`, bound to the
    world's Value pattern read); an unread value rejects whatever the observer saw. Whether
    Chrome's native-select ComboBox gives a readable value is a live fact this cannot pin
    offline: the stage 1 runs measure it (an unread one ends not done, never a false done)."""
    assert check_rejects(task_id, None) == ("value_unread",)
    assert check_rejects(task_id, ValueFact(filled=True, digest="d:other")) == ("value_mismatch",)


def check_rejects(task_id: str, fact: ValueFact | None) -> tuple[str, ...]:
    return _value_claim(task_id, lambda _element: fact)


# ---- the 24 gallery outcomes, rated from the gallery's own saved read -------------------------

PINNED = {
    "g01": ("never_done", None, "R0"),
    "g02": ("done", None, "R1"),
    "g03": ("done", None, "R1"),
    "g04": ("done", None, "R1"),
    "g05": ("done", None, "R1"),
    "g06": ("done", None, "R1"),
    "g07": ("done", None, "R1"),
    "g08": ("done", None, "R1"),
    "g09": ("done", None, "R1"),
    "g10": ("done", None, "R1"),
    "g11": ("done", None, "R1"),
    "g12": ("done", None, "R1"),
    "g13": ("done", None, "R1"),
    "g14": ("done", None, "R1"),
    "g15": ("done", None, "R1"),
    "g16": ("done", None, "R1"),
    "g17": ("blocked", COMMIT_REASON, "R2"),
    "g18": ("blocked", COMMIT_REASON, "R2"),
    "g19": ("blocked", COMMIT_REASON, "R2"),
    "g20": ("done", None, "R1"),
    "g21": ("done", None, "R1"),
    "g22": ("done", None, "R0"),
    "g23": ("done", None, "R1"),
    "g25": ("done", None, "R0"),
}
"""The expected step 3 outcome of each gallery goal row, its reason and its rated tier."""


def test_the_24_gallery_outcomes_from_the_saved_read() -> None:
    read = observed_from_json(READ.read_text("utf-8"))
    got = gallery_expected(read)
    assert {k: (v.outcome, v.reason, v.risk) for k, v in got.items()} == PINNED


def test_the_table_prints_one_line_per_task() -> None:
    lines = table({**large_expected()})
    assert lines[0].split() == ["task", "risk", "outcome", "reason", "proof", "why"]
    assert [line.split()[0] for line in lines[1:]] == ["lg1", "lg2"]
