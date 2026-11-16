"""[V2.54], [V2.55]: the listing cap wired through the prompt, the runner and the outcome. The
model sees only the selected rows; a cut element keeps its handle but the model cannot name it;
the rendered prompt is checked against a token ceiling before any model call."""

from __future__ import annotations

import dataclasses
import json

import pytest

from steerai.loop2 import controller
from steerai.loop2.controller import (
    PROMPT_VERSION,
    SYSTEM_RULES,
    ControllerError,
    ListingRow,
    build_prompt,
    decide,
)
from steerai.loop2.decide_port import DecideCalls, Reply, TaskGenerations
from steerai.loop2.listing import LISTING_CAP, Listing, RowClass
from steerai.loop2.loop import Decided, TokenUsage, TurnView
from steerai.loop2.prompt_inputs import controller_inputs, make_decide
from steerai.loop2.runner import run_task
from steerai.loop2.task import GoalSpan, Subgoal
from steerai.world.elements import Element
from tests.loop2.world_fakes import (
    DOC,
    FIELD,
    GO,
    World,
    act,
    decision,
    observed,
    report,
    task,
)

GOAL = "search the shop for red shoes"


def _rows(n: int, start: int = 100) -> list[Element]:
    return [
        Element(
            id=start + i,
            depth=1,
            role="Button",
            name=f"Item {i}",
            box=(0, 1000 + 10 * i, 100, 1008 + 10 * i),
            runtime_id=(9, start + i),
            parent_id=DOC.id,
        )
        for i in range(n)
    ]


BIG = observed([DOC, FIELD, GO, *_rows(400)], focus=FIELD.runtime_id)


def _view(shown: object = BIG, **changes: object) -> TurnView:
    page = shown if isinstance(shown, type(BIG)) else BIG
    handles = {e.id: e.id for e in page.snapshot.elements}
    view = TurnView(
        observed=page,
        log=(),
        first_turn=True,
        handles=handles,
    )
    return dataclasses.replace(view, **changes)  # type: ignore[arg-type]  # test overrides


# ---- the prompt inputs -------------------------------------------------------------------------


def test_the_prompt_lists_at_most_the_cap_and_only_shown_handles_are_listed() -> None:
    inputs = controller_inputs(_view(), goal=GOAL)
    assert len(inputs.listing_rows) <= LISTING_CAP
    shown = {r.handle for r in inputs.listing_rows}
    assert inputs.listed_ids == frozenset(h for h in shown if h is not None)
    assert 399 + 100 not in inputs.listed_ids  # the last row is cut
    assert inputs.listing is not None
    assert inputs.listing.cut > 0


def test_the_cut_is_said_in_a_header_line_outside_the_screen_fence() -> None:
    inputs = controller_inputs(_view(), goal=GOAL)
    listing = inputs.listing
    assert listing is not None
    assert inputs.listing_note == (
        f"listing: shown {len(listing.ids)} of {listing.eligible} "
        f"(cap {LISTING_CAP}, always-keep up to {2 * LISTING_CAP}, "
        f"goal words up to {LISTING_CAP // 3})"
    )


def test_no_header_line_when_nothing_is_cut() -> None:
    small = observed([DOC, FIELD, GO], focus=FIELD.runtime_id)
    inputs = controller_inputs(_view(small), goal=GOAL)
    assert inputs.listing_note is None


def test_always_keep_from_the_view_reaches_the_listing() -> None:
    far = (9, 100 + 390)
    inputs = controller_inputs(_view(keep=frozenset({far})), goal=GOAL)
    assert 490 in inputs.listed_ids


def test_open_subgoal_words_reach_the_listing() -> None:
    lamp = dataclasses.replace(_rows(1, start=900)[0], name="Red shoes, size 9")
    page = observed([DOC, FIELD, GO, *_rows(400), lamp], focus=FIELD.runtime_id)
    sub = Subgoal(id="s1", desc="x", params={"text": GoalSpan.of(GOAL, 20, 29)}, closes="page")
    inputs = controller_inputs(_view(page, subgoals=(sub,)), goal=GOAL)
    assert 900 in inputs.listed_ids


def test_a_proven_subgoals_words_are_not_goal_words() -> None:
    lamp = dataclasses.replace(_rows(1, start=900)[0], name="Red shoes, size 9")
    page = observed([DOC, FIELD, GO, *_rows(400), lamp], focus=FIELD.runtime_id)
    sub = Subgoal(id="s1", desc="x", params={"text": GoalSpan.of(GOAL, 20, 29)}, closes="page")
    view = _view(page, subgoals=(sub,), proven=frozenset({"s1"}))
    assert 900 not in controller_inputs(view, goal=GOAL).listed_ids


# ---- the prompt guard ([V2.55] (2)) ------------------------------------------------------------


def _ask_recorder(calls: list[list[str]]) -> controller.AskModel:
    def ask(_system: list[str], blocks: list[str]) -> str:
        calls.append(blocks)
        return json.dumps({"stop": "cannot"})

    return ask


def _decide_with(rows: list[ListingRow], ceiling: int, monkeypatch: pytest.MonkeyPatch) -> tuple[
    list[list[str]], list[tuple[frozenset[int], int]]
]:  # fmt: skip
    monkeypatch.setattr(controller, "PROMPT_TOKEN_CEILING", ceiling)
    calls: list[list[str]] = []
    fits: list[tuple[frozenset[int], int]] = []
    decide(
        _ask_recorder(calls),
        task_goal=GOAL,
        subgoal_lines=[],
        action_log=[],
        subgoal_status=[],
        window_title="Shop",
        listing_rows=rows,
        listed_ids=frozenset(r.handle for r in rows if r.handle is not None),
        first_turn=True,
        task_fence="taskfence01",
        on_fit=lambda shown, cut: fits.append((shown, cut)),
    )
    return calls, fits


def _long_rows() -> list[ListingRow]:
    keep: list[RowClass] = ["keep"] * 10
    goal: list[RowClass] = ["goal"] * 10
    rest: list[RowClass] = ["rest"] * 80
    classes = keep + goal + rest
    return [
        ListingRow(1, f'{i} Button "{"x" * 100}"', handle=i, row_class=c)
        for i, c in enumerate(classes, start=1)
    ]


def test_a_prompt_over_the_ceiling_is_cut_rest_first_before_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _long_rows()
    system, user = build_prompt(
        GOAL, [], [], [], "Shop", [], task_fence="taskfence01", screen_fence="f" * 32
    )
    ceiling = controller.prompt_tokens(system, user) + 2_700  # room for about 28 rows
    calls, fits = _decide_with(rows, ceiling=ceiling, monkeypatch=monkeypatch)
    assert len(calls) == 1
    shown, cut = fits[0]
    assert cut > 0
    assert set(range(1, 21)) <= shown  # keep and goal rows survive while rest rows can go
    assert not set(range(81, 101)) & shown  # the rest goes from the bottom up


def test_a_prompt_that_cannot_fit_is_never_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _long_rows()
    with pytest.raises(ControllerError) as refused:
        _decide_with(rows, ceiling=10, monkeypatch=monkeypatch)
    assert refused.value.code == "prompt_too_large"
    assert refused.value.record is None  # no call was made


def test_a_prompt_under_the_ceiling_is_sent_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _long_rows()[:5]
    calls, fits = _decide_with(rows, ceiling=1_000_000, monkeypatch=monkeypatch)
    assert fits == [(frozenset(range(1, 6)), 0)]
    assert len(calls) == 1


def test_a_cut_row_is_no_longer_nameable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The validate set follows the rows actually sent, so a row the guard cut is unlisted."""
    rows = _long_rows()
    system, user = build_prompt(
        GOAL, [], [], [], "Shop", rows[:30], task_fence="taskfence01", screen_fence="f" * 32
    )
    monkeypatch.setattr(controller, "PROMPT_TOKEN_CEILING", controller.prompt_tokens(system, user))

    def ask(_system: list[str], _blocks: list[str]) -> str:
        return json.dumps({"actions": [{"verb": "click", "target": 100, "subgoal": "s1"}],
                           "subgoals": [{"id": "s1", "desc": "x", "params": {"text": "red shoes"},
                                         "closes": "page"}]})  # fmt: skip

    with pytest.raises(ControllerError) as rejected:
        decide(
            ask,
            task_goal=GOAL,
            subgoal_lines=[],
            action_log=[],
            subgoal_status=[],
            window_title="Shop",
            listing_rows=rows,
            listed_ids=frozenset(r.handle for r in rows if r.handle is not None),
            first_turn=True,
            task_fence="taskfence01",
        )
    assert "id_not_listed" in rejected.value.tokens


# ---- the prompt and the legend ----------------------------------------------------------------


NOTE = "listing: shown 150 of 400 (cap 150, always-keep up to 300, goal words up to 50)"


def test_the_listing_note_is_outside_the_screen_fence() -> None:
    _system, blocks = build_prompt(
        GOAL, [], [], [], "Shop", [], task_fence="taskfence01", screen_fence="screenfence02",
        listing_note=NOTE,
    )  # fmt: skip
    turn = blocks[2]
    assert turn.index("listing: shown 150 of 400") < turn.index("<<<screen ")


def test_the_legend_says_a_cut_element_cannot_be_named() -> None:
    assert PROMPT_VERSION == "controller-12"
    assert (
        "- `listing: shown S of E` means code left E-S elements out to fit; an element not "
        "listed cannot be named." in SYSTEM_RULES
    )


# ---- the runner: always-keep and the counts ------------------------------------------------------


def test_the_runner_keeps_what_it_acted_on_and_validates_against_what_was_shown() -> None:
    world = World(
        [decision(act("click", target=GO.id), first=True), decision(act("click", target=499))],
        effects=[report("none")],
        screen=BIG,
    )
    shown_now: list[TurnView] = []
    ports = world.ports()
    real = ports.decide

    def decide_port(view: TurnView) -> Decided:
        shown_now.append(view)
        decided = real(view)
        inputs = controller_inputs(view, goal=GOAL)
        listing = inputs.listing
        assert listing is not None
        return dataclasses.replace(decided, listing=listing.turn(inputs.listed_ids, 0, None))

    outcome = run_task(task(), dataclasses.replace(ports, decide=decide_port))
    assert GO.runtime_id in shown_now[1].keep
    assert [d.action.verb for d in world.dispatched] == ["click"]  # 499 was cut: refused
    assert "refused:id_not_listed" in world.views[-1].history
    assert len(outcome.listings) >= 2
    assert outcome.listings[0].cut > 0


def test_make_decide_reports_the_listing_and_its_prompt_tokens() -> None:
    class Provider:
        def __call__(self, _system: list[str], _blocks: list[str]) -> Reply:
            return Reply(json.dumps({"stop": "cannot"}), "end_turn", TokenUsage(1234, 5))

    calls = DecideCalls(
        Provider(), interrupted=lambda: None, generations=TaskGenerations(),
        late_error=lambda _t: None,
    )  # fmt: skip
    decided = make_decide(calls, goal=GOAL, task_fence="taskfence01")(_view())
    assert decided.listing is not None
    assert decided.listing.cut > 0
    assert decided.listing.prompt_cut == 0
    assert decided.listing.prompt_tokens == 1234
    assert 499 not in decided.listing.listed


# ---- #359 review: the guard keeps always-keep up to the cap; the note counts what was sent ----


def _keep_rows(n: int) -> list[ListingRow]:
    return [ListingRow(1, f'{i} Button "{"x" * 100}"', handle=i, row_class="keep")
            for i in range(1, n + 1)]  # fmt: skip


def test_the_guard_drops_only_always_keep_rows_beyond_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _keep_rows(10)
    system, user = build_prompt(
        GOAL, [], [], [], "Shop", [], task_fence="taskfence01", screen_fence="f" * 32
    )
    monkeypatch.setattr(controller, "PROMPT_TOKEN_CEILING", controller.prompt_tokens(system, user))
    with pytest.raises(ControllerError) as refused:
        decide(
            _ask_recorder([]), task_goal=GOAL, subgoal_lines=[], action_log=[], subgoal_status=[],
            window_title="Shop", listing_rows=rows, listed_ids=frozenset(range(1, 11)),
            first_turn=True, task_fence="taskfence01", keep_cap=6,
        )  # fmt: skip
    assert refused.value.code == "prompt_too_large"  # never below the cap's six


def test_the_guard_drops_always_keep_from_the_bottom_down_to_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _keep_rows(10)
    system, user = build_prompt(
        GOAL, [], [], [], "Shop", rows[:7], task_fence="taskfence01", screen_fence="f" * 32
    )
    monkeypatch.setattr(controller, "PROMPT_TOKEN_CEILING", controller.prompt_tokens(system, user))
    fits: list[tuple[frozenset[int], int]] = []
    decide(
        _ask_recorder([]), task_goal=GOAL, subgoal_lines=[], action_log=[], subgoal_status=[],
        window_title="Shop", listing_rows=rows, listed_ids=frozenset(range(1, 11)),
        first_turn=True, task_fence="taskfence01", keep_cap=6,
        on_fit=lambda shown, cut: fits.append((shown, cut)),
    )  # fmt: skip
    shown, _cut = fits[0]
    assert set(range(1, 7)) <= shown
    assert not {9, 10} & shown


def test_the_note_counts_the_rows_left_after_the_guard() -> None:
    listing = Listing(ids=tuple(range(10)), priorities=("rest",) * 10, eligible=40, cap=150,
                      keep_at_ceiling=False, goal_at_subcap=False)  # fmt: skip
    assert listing.note(4) == (
        "listing: shown 6 of 40 (cap 150, always-keep up to 300, goal words up to 50); "
        "the prompt size limit cut 4"
    )
    whole = dataclasses.replace(listing, eligible=10)
    assert whole.note() is None
    assert whole.note(3) == (
        "listing: shown 7 of 10 (cap 150, always-keep up to 300, goal words up to 50); "
        "the prompt size limit cut 3"
    )


def test_the_prompt_sent_after_a_guard_cut_carries_the_post_cut_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _long_rows()
    listing = Listing(ids=tuple(range(1, 101)), priorities=tuple(r.row_class for r in rows),
                      eligible=100, cap=150, keep_at_ceiling=False,
                      goal_at_subcap=False)  # fmt: skip
    system, user = build_prompt(
        GOAL, [], [], [], "Shop", [], task_fence="taskfence01", screen_fence="f" * 32
    )
    monkeypatch.setattr(
        controller, "PROMPT_TOKEN_CEILING", controller.prompt_tokens(system, user) + 2_000
    )
    calls: list[list[str]] = []
    fits: list[tuple[frozenset[int], int]] = []
    decide(
        _ask_recorder(calls), task_goal=GOAL, subgoal_lines=[], action_log=[], subgoal_status=[],
        window_title="Shop", listing_rows=rows, listed_ids=frozenset(range(1, 101)),
        first_turn=True, task_fence="taskfence01", listing_note_after=listing.note,
        on_fit=lambda shown, cut: fits.append((shown, cut)),
    )  # fmt: skip
    shown, cut = fits[0]
    assert cut > 0
    assert f"listing: shown {len(shown)} of 100" in calls[0][2]


def test_the_estimate_never_undercounts_the_worst_measured_page() -> None:
    """[V2.61]: count_tokens on claude-opus-5, 2026-11-15, the v2 prompt at the structural
    maximum in six scripts: tokens per byte up to 0.7388 (symbols). The bound's ratio is over
    the worst, so the estimate is never low on those prompts; the ceiling is far above the
    largest capped prompt measured on pages (8,339 tokens, #359 review (5))."""
    assert controller.TOKENS_PER_BYTE >= 0.7388
    assert controller.PROMPT_TOKEN_CEILING >= 5 * 8_339


def test_the_guard_never_cuts_the_root_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """#360 item 1: the root chain sits above every other row; cutting it would orphan them."""
    root = [ListingRow(d, f'{d + 1} Pane "{"x" * 100}"', handle=d + 1, row_class="root")
            for d in range(5)]  # fmt: skip
    rows = [*root, *_keep_rows(10)[5:]]
    system, user = build_prompt(
        GOAL, [], [], [], "Shop", root, task_fence="taskfence01", screen_fence="f" * 32
    )
    monkeypatch.setattr(controller, "PROMPT_TOKEN_CEILING", controller.prompt_tokens(system, user))
    fits: list[tuple[frozenset[int], int]] = []
    decide(
        _ask_recorder([]), task_goal=GOAL, subgoal_lines=[], action_log=[], subgoal_status=[],
        window_title="Shop", listing_rows=rows, listed_ids=frozenset(range(1, 11)),
        first_turn=True, task_fence="taskfence01", keep_cap=0,
        on_fit=lambda shown, cut: fits.append((shown, cut)),
    )  # fmt: skip
    assert fits == [(frozenset(range(1, 6)), 5)]
    monkeypatch.setattr(controller, "PROMPT_TOKEN_CEILING", 1)
    with pytest.raises(ControllerError) as refused:
        decide(
            _ask_recorder([]), task_goal=GOAL, subgoal_lines=[], action_log=[], subgoal_status=[],
            window_title="Shop", listing_rows=root, listed_ids=frozenset(range(1, 6)),
            first_turn=True, task_fence="taskfence01", keep_cap=0,
        )  # fmt: skip
    assert refused.value.code == "prompt_too_large"
