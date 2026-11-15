"""[V2.65] (1), option (b): a target that shows only after an opener is read after one scripted
click on that opener, in the harness's own fresh profile, never Steer's input. The click goes to
exactly the element the openers data names, found in the page's accessibility tree, and calls
the element's own click; nothing is typed or submitted. The revealed state is taken only when it
shows the same new elements on two reads in a row within the bound; otherwise the task stays
unrated. Fakes only."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from steerai.loop2.stage1_expected import Act
from steerai.loop2.stage2_expected import (
    OPENERS,
    DecidingFacts,
    OpenerError,
    SiteRow,
    click_opener,
    fresh_opened,
    load_openers,
    opened_read_path,
    opener_start_facts,
    opener_table_row,
    reveal_targets,
    wait_revealed,
)
from steerai.world.effects import Observed
from steerai.world.read_json import observed_to_json
from tests.loop2.world_fakes import DOC, FIELD, element, observed


def ax(role: str, name: str, node: int, *, ignored: bool = False) -> dict[str, Any]:  # Any: JSON
    return {
        "nodeId": str(node),
        "ignored": ignored,
        "role": {"type": "role", "value": role},
        "name": {"type": "computedString", "value": name},
        "backendDOMNodeId": node,
    }


class Cdp:
    def __init__(self, nodes: list[dict[str, Any]]) -> None:  # Any: JSON
        self.nodes = nodes
        self.calls: list[tuple[str, Mapping[str, Any]]] = []  # Any: JSON params

    def __call__(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, dict(params or {})))
        if method == "Accessibility.getFullAXTree":
            return {"nodes": self.nodes}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": f"obj-{params['backendNodeId'] if params else 0}"}}
        return {}


OPEN_SEARCH = Act("click", "Button", "Search the site")


def test_the_opener_is_clicked_by_its_own_click_once() -> None:
    cdp = Cdp([ax("button", "Search the site", 7), ax("button", "Menu", 8)])
    click_opener(cdp, OPEN_SEARCH)
    methods = [m for m, _ in cdp.calls]
    assert methods[-2:] == ["DOM.resolveNode", "Runtime.callFunctionOn"]
    assert cdp.calls[-2][1] == {"backendNodeId": 7}
    clicked = cdp.calls[-1][1]
    assert clicked["objectId"] == "obj-7"
    assert "click()" in clicked["functionDeclaration"]
    assert not any("Input." in m for m in methods)  # no OS-level or CDP input events


@pytest.mark.parametrize(
    ("nodes", "reason"),
    [
        ([ax("button", "Menu", 8)], "opener_not_found"),
        (
            [ax("button", "Search the site", 7), ax("button", "Search the site", 9)],
            "opener_ambiguous",
        ),
        ([ax("button", "Search the site", 7, ignored=True)], "opener_not_found"),
        ([ax("link", "Search the site", 7)], "opener_not_found"),
    ],
)
def test_an_opener_not_found_exactly_once_is_never_clicked(
    nodes: Any, reason: str
) -> None:  # Any: JSON
    cdp = Cdp(nodes)
    with pytest.raises(OpenerError, match=reason):
        click_opener(cdp, OPEN_SEARCH)
    assert "Runtime.callFunctionOn" not in [m for m, _ in cdp.calls]


def test_a_tree_item_opener_is_found_by_its_tree_role() -> None:
    cdp = Cdp([ax("treeitem", "Reports", 3)])
    click_opener(cdp, Act("expand", "TreeItem", "Reports"))
    assert cdp.calls[-2][1] == {"backendNodeId": 3}


# ---- the revealed state: new elements, the same on two reads in a row ---------------------------

BOX = element(20, "Edit", "Find", (9, 20), 1)
GO = element(21, "Button", "Go", (9, 21), 1)
BEFORE = observed([DOC, FIELD])
AFTER = observed([DOC, FIELD, BOX, GO])
MOVING = observed([DOC, FIELD, BOX])


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def reads(*sequence: Observed) -> Any:  # Any: a read callable
    queue = list(sequence)
    return lambda: queue.pop(0) if len(queue) > 1 else queue[0]


def test_a_reveal_seen_twice_in_a_row_is_taken() -> None:
    clock = Clock()
    got = wait_revealed(reads(BEFORE, AFTER, AFTER), BEFORE, clock=clock, sleep=clock.sleep)
    assert got is not None
    assert got.read == AFTER
    assert got.new_roles == {"Button": 1, "Edit": 1}


def test_a_reveal_that_keeps_moving_is_not_taken_until_it_settles() -> None:
    clock = Clock()
    got = wait_revealed(reads(MOVING, AFTER, AFTER), BEFORE, clock=clock, sleep=clock.sleep)
    assert got is not None
    assert got.read == AFTER


def test_no_reveal_within_the_bound_leaves_the_task_unrated() -> None:
    clock = Clock()
    assert wait_revealed(reads(BEFORE), BEFORE, clock=clock, sleep=clock.sleep) is None
    assert clock.now <= 15.0


# ---- the openers data ---------------------------------------------------------------------------


def test_the_openers_are_data_for_each_task_held_after_an_opener() -> None:
    assert set(OPENERS) == {"gh1", "mdn1", "u3", "u5", "m1", "r4"}
    assert all(a.verb in {"click", "expand"} and a.role and a.name for a in OPENERS.values())


@pytest.mark.parametrize(
    "bad",
    [
        {"t1": {"verb": "type", "role": "Edit", "name": "x"}},
        {"t1": {"verb": "click", "role": "", "name": "x"}},
        {"t1": {"verb": "click", "role": "Button", "name": "x", "param": "y"}},
    ],
)
def test_an_opener_that_is_not_one_named_click_is_refused(bad: object) -> None:
    with pytest.raises(ValueError, match="openers"):
        load_openers(json.dumps(bad))


# ---- rating and the run's start check for a task behind an opener -------------------------------


def test_a_task_behind_an_opener_is_rated_on_its_opened_read(tmp_path: Path) -> None:
    assert opened_read_path(tmp_path, "mdn1") == tmp_path / "mdn1.opened.json"
    assert opened_read_path(tmp_path, "wk1") == tmp_path / "wk1.json"


FACTS = DecidingFacts("Edit", "searchbox", True, "Button", True, "R1")
OPENER_BUTTON = element(30, "Button", "Search the site", (9, 30), 1)


def test_the_start_check_behind_an_opener_reads_only_the_opener() -> None:
    """The revealed target is not on the start page: the run's start check finds the opener
    once, and the committed facts stand for the revealed state."""
    start = observed([DOC, FIELD, OPENER_BUTTON])
    assert opener_start_facts(OPEN_SEARCH, start, FACTS) == FACTS
    assert opener_start_facts(OPEN_SEARCH, observed([DOC, FIELD]), FACTS) is None
    twice = observed(
        [DOC, FIELD, OPENER_BUTTON, element(31, "Button", "Search the site", (9, 31), 1)]
    )
    assert opener_start_facts(OPEN_SEARCH, twice, FACTS) is None


def test_only_new_elements_inside_the_page_count_as_revealed() -> None:
    """The browser's own frame changes on its own (a tab's title, a notice button): never a
    reveal. Only new elements under the page's Document count (the slot's first pass)."""
    frame = element(90, "Pane", "Browser", (9, 90), None)
    notice = element(91, "Button", "Action required", (9, 91), 90)
    clock = Clock()
    noisy = observed([DOC, FIELD, frame, notice])
    assert wait_revealed(reads(noisy, noisy), observed([DOC, FIELD, frame]), clock=clock,
                         sleep=clock.sleep) is None  # fmt: skip


def test_a_row_behind_an_opener_rates_the_opener_on_the_start_and_the_rest_when_opened() -> None:
    """The opener may vanish once clicked (a search button replaced by its field): it is rated
    on the start read, the rest on the opened read, and the row's risk is the higher."""
    field = element(40, "ComboBox", "Search", (9, 40), 1)
    start = observed([DOC, FIELD, OPENER_BUTTON])
    shown = [DOC, FIELD, field]
    opened = dataclasses.replace(
        observed(shown),
        aria_roles={field.runtime_id: "combobox"},
        landmarks={e.runtime_id: None for e in shown},
    )
    row = SiteRow(
        acts=(
            OPEN_SEARCH,
            Act("type", "ComboBox", "Search", param="flexbox"),
            Act("press", "", "", key="Enter"),
        ),
        proof="navigated",
        why="w",
    )
    got = opener_table_row("mdn1", row, "search the docs for flexbox", saved(start), saved(opened))
    assert got.facts.field_role == "ComboBox"
    assert got.facts.aria_role == "combobox"
    assert got.read_sha256 == hashlib.sha256(saved(opened)).hexdigest()


def test_a_row_behind_an_opener_must_start_with_that_opener() -> None:
    row = SiteRow((Act("type", "ComboBox", "Search", param="x"),), None, "w")
    with pytest.raises(ValueError, match="opener"):
        opener_table_row("mdn1", row, "g", saved(observed([DOC])), saved(observed([DOC])))


def saved(seen: Observed) -> bytes:
    return (observed_to_json(seen) + "\n").encode("utf-8")


# ---- #424 (1): a stale opened read never survives a later failed reveal --------------------------


def test_a_new_attempt_removes_the_earlier_opened_read(tmp_path: Path) -> None:
    stale = tmp_path / "mdn1.opened.json"
    stale.write_text("{}", encoding="utf-8")
    target = fresh_opened(tmp_path, "mdn1")
    assert target == stale
    assert not stale.exists()  # an unstable reveal now leaves no file to be rated


# ---- #424 (3): the click and the start check count copies by one rule ---------------------------


def nested(child: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:  # Any: JSON
    return {**child, "parentId": parent["nodeId"]}


def test_a_copy_nested_in_a_same_named_opener_is_one_opener() -> None:
    """As `outermost` folds a same-named element inside another for the start check, the click
    folds it too: the outer one is clicked."""
    outer = ax("button", "Search the site", 7)
    cdp = Cdp([outer, nested(ax("button", "Search the site", 8), outer)])
    click_opener(cdp, OPEN_SEARCH)
    assert cdp.calls[-2][1] == {"backendNodeId": 7}


def test_a_tree_item_inside_a_same_named_tree_item_is_another_item() -> None:
    """`outermost` never folds a TreeItem (a folder in a same-named folder): neither does the
    click, so two are ambiguous and nothing is clicked."""
    outer = ax("treeitem", "Reports", 3)
    cdp = Cdp([outer, nested(ax("treeitem", "Reports", 4), outer)])
    with pytest.raises(OpenerError, match="opener_ambiguous"):
        click_opener(cdp, Act("expand", "TreeItem", "Reports"))


# ---- #424 (4): the reveal must show the task's own target -----------------------------------


def test_a_stable_reveal_without_the_target_is_not_taken() -> None:
    """A cookie banner settles too: only a reveal that shows the row's own target counts."""
    clock = Clock()
    banner = observed([DOC, FIELD, element(50, "Button", "Accept cookies", (9, 50), 1)])
    target = (Act("type", "Edit", "Find", param="x"),)
    got = wait_revealed(
        reads(banner, banner), BEFORE, clock=clock, sleep=clock.sleep, targets=target
    )
    assert got is None


def test_a_stable_reveal_with_the_target_is_taken() -> None:
    clock = Clock()
    target = (Act("type", "Edit", "Find", param="x"), Act("press", "", "", key="Enter"))
    got = wait_revealed(reads(AFTER, AFTER), BEFORE, clock=clock, sleep=clock.sleep, targets=target)
    assert got is not None


def test_the_reveal_targets_are_the_acts_after_the_opener() -> None:
    row = SiteRow(
        (OPEN_SEARCH, Act("type", "Edit", "Find", param="x"), Act("press", "", "", key="Enter")),
        "navigated",
        "w",
    )
    assert reveal_targets(row) == (Act("type", "Edit", "Find", param="x"),)
