"""H2, the fresh held-out Phase 1 set (design 12 [v2.45]): committed before the #147-#151
fixes, run only after them; mostly shapes and apps the development set lacks. Pure parts: the
tasks, the one-event rule, the button-name approvals, the Calculator and saved-file reads."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from steerai.probes.h2_page import LOG_KEY, page_html
from steerai.probes.task_suite import (
    H2,
    HELD_OUT,
    MULTIPLY,
    TASKS,
    Person,
    calc_display,
    h2_event,
    passed,
    task_set,
)


def _task(task_id: str) -> Any:
    return next(t for t in H2 if t.task_id == task_id)


def test_ten_fresh_tasks_never_seen_before() -> None:
    assert len(H2) == 10
    ids = {t.task_id for t in H2}
    assert len(ids) == 10
    assert not ids & ({t.task_id for t in TASKS} | {t.task_id for t in HELD_OUT})
    assert task_set("h2") == H2


def test_the_page_logs_under_its_key() -> None:
    assert LOG_KEY in page_html()
    assert "isTrusted" in page_html()


def test_exactly_one_trusted_event_equal_to_the_expected_one() -> None:
    expected = {"kind": "todo_add", "text": "buy milk"}
    assert h2_event(json.dumps([expected])) == json.dumps(expected, sort_keys=True)
    two = json.dumps([expected, {"kind": "todo_delete", "text": "call mom"}])
    assert h2_event(two) == "events:2"
    untrusted = json.dumps([{**expected, "untrusted": True}])
    assert h2_event(untrusted) == "untrusted"
    t1 = _task("t1")
    assert passed(t1, "done", h2_event(json.dumps([expected])))
    assert not passed(t1, "done", h2_event(json.dumps([{"kind": "todo_add", "text": "buy"}])))


def test_a_button_ask_is_approved_only_for_its_exact_name() -> None:
    person = Person(frozenset(), frozenset({"Delete call mom"}))

    def ask(name: str, text: str | None = None) -> Any:
        return SimpleNamespace(
            decision_id="d1",
            params={"text": text},
            verb="delete",
            risk="R3",
            kind="yes",
            target_name=name,
        )

    assert person.ask(ask("Delete call mom")) == "yes:d1"
    assert person.ask(ask("Delete pay rent")) == "no:d1"
    assert person.ask(ask("Delete call mom", text="anything")) == "no:d1", "text asks need text"


def test_the_calculator_must_show_the_computation_not_just_the_number() -> None:
    """A typed 84 is not 12 times 7 (#154 review item 1): the expression must show it."""
    computed = calc_display(
        [
            ("CalculatorResults", "Display is 84"),
            ("CalculatorExpression", f"Expression is 12 {MULTIPLY} 7="),
        ]
    )
    assert passed(_task("calc1"), "done", computed)
    typed = calc_display([("CalculatorResults", "Display is 84")])
    assert not passed(_task("calc1"), "done", typed)
    wrong = calc_display(
        [("CalculatorResults", "Display is 84"), ("CalculatorExpression", "Expression is 84=")]
    )
    assert not passed(_task("calc1"), "done", wrong)


def test_goals_state_outcomes_not_steps() -> None:
    """m1 and l1 never name the control to click (#154 review item 3)."""
    assert "click" not in _task("m1").goal.casefold()
    assert "click" not in _task("l1").goal.casefold()
    assert "menu" not in _task("m1").goal.casefold()


def test_a_malformed_log_is_a_failed_row_not_a_crash() -> None:
    assert h2_event("not json") == "bad_log"
    assert h2_event("{}") == "bad_log"


def test_the_saved_file_must_hold_exactly_the_text(tmp_path: Path) -> None:
    save1 = _task("save1")
    assert passed(save1, "done", "steer h2 saved")
    assert not passed(save1, "done", "steer h2 saved and more")
    assert not passed(save1, "done", "file_missing")
    assert not passed(save1, "done", "saved_to_documents")
