"""expand / collapse on a tree item in the semantic executor ([v2.81] rule 2): Chrome ignores
Expand on an ARIA tree item (measured 3/3, Chrome 156 and 157), so the executor reads the state
and refuses `expand_by_click` with nothing sent; the loop then clicks once. Never an Expand: if
Chrome began honouring it, Expand followed by the click would open and close."""

from __future__ import annotations

import pytest

from steerai.action.dispositions import Delivery
from steerai.action.executor import Action
from steerai.action.tree import expand_decision
from steerai.errors import ErrorCode
from tests.action.fakes import UIA_EXPAND, FakeHandle
from tests.action.test_executor import _executor, _snapshot, fast_clock  # noqa: F401 - fixture

_COLLAPSED, _EXPANDED, _PARTIAL, _LEAF = 0, 1, 2, 3


@pytest.mark.parametrize(
    ("verb", "state", "detail"),
    [
        ("expand", _COLLAPSED, "expand_by_click"),
        ("collapse", _EXPANDED, "expand_by_click"),
        ("expand", _LEAF, "leaf_node"),
        ("collapse", _LEAF, "leaf_node"),
        ("expand", _PARTIAL, "partial_state"),
        ("collapse", _PARTIAL, "partial_state"),
    ],
)
def test_a_tree_item_out_of_state_is_refused_with_nothing_sent(
    fast_clock: None,  # noqa: F811
    verb: str,
    state: int,
    detail: str,
) -> None:
    handle = FakeHandle(patterns={UIA_EXPAND}, expanded=state)
    result = _executor().execute(
        Action(verb=verb, element_id=1), _snapshot(handle, role="TreeItem")
    )
    assert handle.calls == [], "never Expand nor Collapse on a tree item"
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.steps == []
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == detail


@pytest.mark.parametrize(("verb", "state"), [("expand", _EXPANDED), ("collapse", _COLLAPSED)])
def test_a_tree_item_in_state_is_already_satisfied(
    fast_clock: None,  # noqa: F811
    verb: str,
    state: int,
) -> None:
    handle = FakeHandle(patterns={UIA_EXPAND}, expanded=state)
    result = _executor().execute(
        Action(verb=verb, element_id=1), _snapshot(handle, role="TreeItem")
    )
    assert handle.calls == []
    assert result.skipped is True
    assert result.ok is True


def test_a_tree_item_without_the_pattern_is_refused(fast_clock: None) -> None:  # noqa: F811
    handle = FakeHandle(patterns=set())
    result = _executor().execute(
        Action(verb="expand", element_id=1), _snapshot(handle, role="TreeItem")
    )
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.evidence["detail"] != "expand_by_click"


def test_a_disclosure_button_still_expands_through_the_pattern(fast_clock: None) -> None:  # noqa: F811
    """W5 unchanged: a Button's Expand works (measured), only a tree item clicks."""
    handle = FakeHandle(patterns={UIA_EXPAND}, expanded=_COLLAPSED)
    result = _executor().execute(
        Action(verb="expand", element_id=1), _snapshot(handle, role="Button")
    )
    assert handle.calls == ["expand"]
    assert result.delivery is Delivery.DELIVERED_VERIFIED


@pytest.mark.parametrize(
    ("verb", "state", "decision"),
    [
        ("expand", None, "no_pattern"),
        ("expand", _EXPANDED, "in_state"),
        ("collapse", _COLLAPSED, "in_state"),
        ("expand", _COLLAPSED, "click"),
        ("collapse", _EXPANDED, "click"),
        ("expand", _LEAF, "leaf_node"),
        ("collapse", _PARTIAL, "partial_state"),
        ("expand", 7, "partial_state"),
    ],
)
def test_the_one_decision_the_executor_and_the_loop_share(
    verb: str, state: int | None, decision: str
) -> None:
    assert expand_decision(state, verb) == decision
