"""[V2.65] (1), option (b): a target that shows only after an opener is read after one scripted
click on that opener, in the harness's own fresh profile, never Steer's input. The click goes to
exactly the element the openers data names, found in the page's accessibility tree, and calls
the element's own click; nothing is typed or submitted. The revealed state is taken only when it
shows the same new elements on two reads in a row within the bound; otherwise the task stays
unrated. Fakes only."""

from __future__ import annotations

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
    click_opener,
    load_openers,
    opened_read_path,
    opener_start_facts,
    wait_revealed,
)
from steerai.world.effects import Observed
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
