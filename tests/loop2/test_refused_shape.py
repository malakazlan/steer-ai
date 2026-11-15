"""[V2.69] Three, intake packet rev 5 §4: after a refused first Decision (rejected by `validate`
after its repair, by the intake check, or by its memo) the next intake turn's history carries a
closed `refused_shape` line: the refused reply's subgoals by position and closing kind, so the
model can see what it declared (the prompt is rebuilt every turn). Never model text."""

from __future__ import annotations

import pytest

from steerai.loop2.controller import SYSTEM_RULES, ControllerError
from steerai.loop2.decision import MAX_SUBGOALS, Closes, Decision
from steerai.loop2.history import (
    GRAMMAR,
    POSITIONAL_GRAMMAR,
    RefusedShape,
    Vocab,
    record_ok,
    refused_shape,
    render,
)
from steerai.loop2.runner import run_task
from steerai.loop2.turn_trace import positional
from tests.loop2.test_controller import BAD_TARGET, make_model, run
from tests.loop2.test_intake_check import VALUE, _first, _reject, _rejected_once, _world_with
from tests.loop2.test_rejected_intake_turn import SUBS, _rejected
from tests.loop2.world_fakes import GO, World, act, report, task

VOCAB = Vocab(known={}, issued=frozenset())
PAGE: Closes = "page"


def _shapes(history: tuple[str, ...]) -> list[str]:
    return [line for line in history if line.startswith("refused_shape:")]


# ---- the line ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("closes", "line"),
    [
        (("value", "state"), "refused_shape:1=value,2=state"),
        ((), "refused_shape:none"),
        (None, "refused_shape:unparsed"),
        (("page",) * (MAX_SUBGOALS + 2), "refused_shape:" + ",".join(
            f"{n}=page" for n in range(1, MAX_SUBGOALS + 1)) + ",more=2"),
    ],
    ids=["kinds", "none", "unparsed", "more"],
)  # fmt: skip
def test_each_form_renders_closed_and_passes_both_grammars(
    closes: tuple[Closes, ...] | None, line: str
) -> None:
    record = refused_shape(closes)
    assert render(record) == line
    assert record_ok(record, VOCAB)
    assert POSITIONAL_GRAMMAR.fullmatch(line) is not None  # the redacted trace keeps it as is


@pytest.mark.parametrize(
    "record",
    [
        RefusedShape(("value", "shoes")),  # type: ignore[arg-type]  # a kind outside the set
        RefusedShape(("value",), more=2),  # `more` only after a full set
        RefusedShape(None, more=1),
        RefusedShape(tuple(PAGE for _ in range(MAX_SUBGOALS + 1))),
    ],
    ids=["unknown_kind", "more_short", "more_unparsed", "too_many"],
)
def test_anything_else_is_dropped(record: RefusedShape) -> None:
    assert not record_ok(record, VOCAB)


def test_the_redacted_trace_keeps_the_record_unchanged() -> None:
    """#464 review: the record holds no model names, so `positional` passes it through."""
    record = refused_shape(["value", "state"])
    assert positional(record, ["s1", "s2"], {"s1": frozenset({"text"})}) is record


def test_the_grammar_refuses_free_text_in_the_line() -> None:
    assert GRAMMAR.fullmatch("refused_shape:1=red shoes") is None
    assert GRAMMAR.fullmatch("refused_shape:6=value") is None


def test_the_model_is_told_what_the_line_means() -> None:
    assert "refused_shape lists the subgoals of your refused reply" in SYSTEM_RULES


# ---- the source: the controller's last parsed attempt -----------------------------------------


def test_a_rejected_turn_carries_its_last_parsed_shape() -> None:
    ask, _ = make_model([BAD_TARGET, BAD_TARGET])
    with pytest.raises(ControllerError) as e:
        run(ask)
    assert e.value.shape == ()  # a later-turn reply declares no subgoals


def test_a_last_attempt_that_did_not_parse_has_no_shape() -> None:
    ask, _ = make_model([BAD_TARGET, "not json"])
    with pytest.raises(ControllerError) as e:
        run(ask)
    assert e.value.shape is None


# ---- the runner: each refused first Decision -------------------------------------------------


def test_an_intake_reject_shows_the_declared_kinds_next_turn() -> None:
    world = _rejected_once()
    run_task(task(), world.ports())
    assert _shapes(world.views[1].history) == ["refused_shape:1=value"]


def test_a_memo_reject_shows_them_too() -> None:
    world = _world_with([_first([VALUE])] * 3, reply=_reject())
    run_task(task(), world.ports())
    assert _shapes(world.views[2].history) == ["refused_shape:1=value"] * 2


def test_a_validate_reject_shows_the_last_parsed_shape() -> None:
    world = World(
        [Decision(subgoals=SUBS, actions=[act("click", target=GO.id)]), Decision(stop="blocked")],
        effects=[report("content_changed")],
    )
    error = _rejected("subgoal_id_bad")
    error.shape = ("page",)
    world.decide_errors = [error, None, None]
    run_task(task(), world.ports())
    assert _shapes(world.views[1].history) == ["refused_shape:1=page"]


def test_a_rejected_turn_after_the_freeze_has_no_shape_line() -> None:
    world = World(
        [
            Decision(subgoals=SUBS, actions=[act("click", target=GO.id)]),
            Decision(actions=[act("click", target=GO.id)]),
            Decision(stop="blocked"),
        ],
        effects=[report("content_changed")] * 2,
    )
    error = _rejected("id_not_listed")
    error.shape = ()
    world.decide_errors = [None, error, None]
    run_task(task(), world.ports())
    assert _shapes(world.views[-1].history) == []
