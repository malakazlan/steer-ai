"""`choose` on a ComboBox (widget-verbs W4 v1-v3 and their verdicts): the refusals before
Expand, the option found in the ComboBox's own subtree only, one Select, the polled Collapse on
every path after Expand, and the Value readback. Pure: fake COM handles and a fake option source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

import steerai.action.readback as readback_mod
from steerai.action.dispositions import Delivery
from steerai.action.execution import UiaBindings
from steerai.action.executor import Action, Executor
from steerai.action.options import OptionItem
from steerai.errors import ErrorCode
from steerai.world.elements import Element
from steerai.world.model import Snapshot
from tests.action.fakes import (
    BOX,
    TYPE_IDS,
    UIA_EXPAND,
    UIA_SELECTION_ITEM,
    UIA_VALUE,
    FakeWindows,
    com_error,
)

COLLAPSED, EXPANDED, LEAF = 0, 1, 3
NBSP = chr(0xA0)
E_FAIL = 0x80004005


@dataclass
class Combo:
    """A native select as Chrome 156 exposes it (probe `widget_patterns`): Value and
    ExpandCollapse on the box; Collapse reads Expanded at once and Collapsed after a few reads."""

    value: str | None = "Small"
    state: int = COLLAPSED
    patterns: set[int] = field(default_factory=lambda: {UIA_EXPAND, UIA_VALUE})
    collapse_reads: int = 2
    """Reads that still say Expanded after Collapse is sent (measured: 0 within 25 ms)."""
    collapse_never: bool = False
    collapse_error: Exception | None = None
    expand_error: Exception | None = None
    expand_has_effect: bool = True
    calls: list[str] = field(default_factory=list)
    name: str = "Size"
    control_type: int = TYPE_IDS["ComboBox"]
    _pending: int = 0

    @property
    def CurrentControlType(self) -> int:  # noqa: N802 - COM naming
        return self.control_type

    @property
    def CurrentName(self) -> str:  # noqa: N802
        return self.name

    def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802
        if pattern_id not in self.patterns:
            return None
        combo = self

        class _Pattern:
            def QueryInterface(self, _iface: Any) -> Any:  # noqa: N802
                return self

            @property
            def CurrentValue(self) -> str | None:  # noqa: N802
                return combo.value

            @property
            def CurrentExpandCollapseState(self) -> int:  # noqa: N802
                if combo._pending > 0:
                    combo._pending -= 1
                    return EXPANDED
                return combo.state

            def Expand(self) -> None:  # noqa: N802
                combo.calls.append("expand")
                if combo.expand_error is not None:
                    raise combo.expand_error
                if combo.expand_has_effect:
                    combo.state = EXPANDED

            def Collapse(self) -> None:  # noqa: N802
                combo.calls.append("collapse")
                if combo.collapse_error is not None:
                    raise combo.collapse_error
                if combo.collapse_never:
                    return
                combo.state = COLLAPSED
                combo._pending = combo.collapse_reads

        return _Pattern()


@dataclass
class Item:
    """An option ListItem: SelectionItem.Select sets the box's value to its name."""

    combo: Combo
    name: str
    value: str | None = None
    calls: list[str] = field(default_factory=list)

    def GetCurrentPattern(self, pattern_id: int) -> Any:  # noqa: N802
        if pattern_id != UIA_SELECTION_ITEM:
            return None
        item = self

        class _Pattern:
            def QueryInterface(self, _iface: Any) -> Any:  # noqa: N802
                return self

            def Select(self) -> None:  # noqa: N802
                item.combo.calls.append(f"select:{item.name}")
                item.combo.value = item.value if item.value is not None else item.name

        return _Pattern()


@dataclass
class Options:
    """The option source: the ListItems in the asked ComboBox's own subtree, shown only while
    it reads Expanded, after `appear_after` reads (measured: 17-34 ms after Expand)."""

    subtrees: dict[int, list[OptionItem]] = field(default_factory=dict)
    appear_after: int = 0
    asked: list[Any] = field(default_factory=list)
    reads: int = 0

    def list_items(self, combo: Any) -> list[OptionItem]:
        self.asked.append(combo)
        self.reads += 1
        if combo.state != EXPANDED or self.reads <= self.appear_after:
            return []
        return list(self.subtrees.get(id(combo), []))


def _items(combo: Combo, *names: str, enabled: bool = True) -> list[OptionItem]:
    return [
        OptionItem(name=n, enabled=enabled, selectable=True, handle=Item(combo, n)) for n in names
    ]


def _source(combo: Combo, *items: OptionItem, appear_after: int = 0) -> Options:
    return Options(subtrees={id(combo): list(items)}, appear_after=appear_after)


def _executor(options: Options | None, blocker: Any = None) -> Executor:
    return Executor(
        uia=UiaBindings(control_types={v: k for k, v in TYPE_IDS.items()}, interfaces={}),
        windows=FakeWindows(),
        clock=lambda: 0.0,
        blocker=blocker,
        options=options,
    )


def _snapshot(combo: Combo) -> Snapshot:
    element = Element(id=1, depth=0, role="ComboBox", name=combo.name, box=BOX, runtime_id=(7, 1))
    return Snapshot(
        id="s1",
        hwnd=0x10,
        process="chrome.exe",
        title="t",
        taken_at=0.0,
        elements=[element],
        node_count=1,
        stable=True,
        handles=[combo],
    )


def _choose(option: str = "Large") -> Action:
    return Action(verb="choose", element_id=1, option=option)


@pytest.fixture
def fast_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    now = {"t": 0.0}

    def sleep(seconds: float) -> None:
        now["t"] += seconds

    monkeypatch.setattr(
        readback_mod, "time", SimpleNamespace(monotonic=lambda: now["t"], sleep=sleep)
    )
    return now


def _labels(result: Any) -> list[str]:
    return [s.label for s in result.steps]


# ---- the happy path ------------------------------------------------------------------------------


def test_choose_expands_selects_collapses_and_reads_the_value_back(fast_clock: Any) -> None:
    combo = Combo()
    options = _source(combo, *_items(combo, "Small", "Large"), appear_after=2)
    result = _executor(options).execute(_choose(), _snapshot(combo))
    assert result.delivery is Delivery.DELIVERED_VERIFIED, result.to_dict()
    assert result.ok is True
    assert combo.calls == ["expand", "select:Large", "collapse"]
    assert _labels(result) == [
        "ExpandCollapsePattern.Expand",
        "SelectionItemPattern.Select",
        "ExpandCollapsePattern.Collapse",
    ]
    assert combo.value == "Large"


def test_the_option_is_searched_in_the_comboboxs_own_subtree_only(fast_clock: Any) -> None:
    # W4-13: a same-named ListItem anywhere else is never chosen
    combo, other = Combo(), Combo(name="Colour")
    options = _source(combo, *_items(combo, "Small"))
    options.subtrees[id(other)] = _items(other, "Large")
    result = _executor(options).execute(_choose(), _snapshot(combo))
    assert all(asked is combo for asked in options.asked)
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.evidence["detail"] == "no_match"
    assert "select:Large" not in combo.calls + other.calls


def test_a_value_differing_only_by_a_no_break_space_is_already_satisfied(fast_clock: Any) -> None:
    # W4-05: NFKC folds U+00A0 (names probe: an NBSP survives in the name and the Value)
    combo = Combo(value=f"New{NBSP}York")
    options = _source(combo)
    result = _executor(options).execute(_choose("New York"), _snapshot(combo))
    assert result.skipped is True
    assert result.ok is True
    assert combo.calls == []
    assert options.asked == []


def test_the_option_name_is_matched_by_the_folding_comparator(fast_clock: Any) -> None:
    combo = Combo()
    item = OptionItem(
        name=f"New{NBSP}York", enabled=True, selectable=True, handle=Item(combo, f"New{NBSP}York")
    )
    result = _executor(_source(combo, item)).execute(_choose("New  York"), _snapshot(combo))
    assert result.delivery is Delivery.DELIVERED_VERIFIED, result.to_dict()


def test_case_is_not_folded(fast_clock: Any) -> None:
    combo = Combo()
    result = _executor(_source(combo, *_items(combo, "large"))).execute(
        _choose("Large"), _snapshot(combo)
    )
    assert result.evidence["detail"] == "no_match"


# ---- refusals before Expand: nothing sent --------------------------------------------------------


def test_a_leaf_node_combobox_is_editable_and_refused_before_expand(fast_clock: Any) -> None:
    # W4 v2: a datalist input reads LeafNode; neither IsReadOnly nor an Edit child tells it apart
    combo = Combo(state=LEAF)
    options = _source(combo, *_items(combo, "Large"))
    result = _executor(options).execute(_choose(), _snapshot(combo))
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.ELEMENT_NOT_ACTIONABLE
    assert result.evidence["detail"] == "editable_combobox"
    assert combo.calls == []
    assert result.steps == []


def test_no_value_pattern_is_refused_before_expand(fast_clock: Any) -> None:
    combo = Combo(patterns={UIA_EXPAND})
    result = _executor(_source(combo)).execute(_choose(), _snapshot(combo))
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.evidence["detail"] == "no_ValuePattern"
    assert combo.calls == []


def test_no_expand_collapse_pattern_is_refused_before_anything(fast_clock: Any) -> None:
    combo = Combo(patterns={UIA_VALUE})
    result = _executor(_source(combo)).execute(_choose(), _snapshot(combo))
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.evidence["detail"] == "no_ExpandCollapsePattern"
    assert combo.calls == []


def test_the_value_already_equal_is_already_satisfied_with_no_expand(fast_clock: Any) -> None:
    combo = Combo(value="Large")
    result = _executor(_source(combo)).execute(_choose(), _snapshot(combo))
    assert result.skipped is True
    assert result.delivery is Delivery.NOT_DELIVERED
    assert combo.calls == []


def test_no_option_source_is_refused_before_expand(fast_clock: Any) -> None:
    combo = Combo()
    result = _executor(None).execute(_choose(), _snapshot(combo))
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.evidence["detail"] == "no_option_source"
    assert combo.calls == []


# ---- an already expanded box: no Expand, still the polled Collapse (W4 v1 verdict item 2) --------


def test_an_already_expanded_box_skips_expand_and_ends_with_the_collapse(fast_clock: Any) -> None:
    combo = Combo(state=EXPANDED)
    result = _executor(_source(combo, *_items(combo, "Large"))).execute(_choose(), _snapshot(combo))
    assert result.delivery is Delivery.DELIVERED_VERIFIED, result.to_dict()
    assert combo.calls == ["select:Large", "collapse"]


# ---- refusals after Expand -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("items", "detail"),
    [
        ("none", "no_match"),
        ("twins", "ambiguous_option"),
        ("disabled", "option_disabled"),
    ],
)
def test_a_refusal_after_expand_collapses_and_is_not_delivered_once_collapsed(
    fast_clock: Any, items: str, detail: str
) -> None:
    combo = Combo()
    shown = {
        "none": _items(combo, "Small", "Medium"),
        "twins": _items(combo, "Large", "Large"),
        "disabled": _items(combo, "Large", enabled=False),
    }[items]
    result = _executor(_source(combo, *shown)).execute(_choose(), _snapshot(combo))
    assert result.delivery is Delivery.NOT_DELIVERED, result.to_dict()
    assert result.retry_safe is True
    assert result.evidence["detail"] == detail
    assert result.evidence["expanded"] is True
    assert combo.calls == ["expand", "collapse"]
    assert _labels(result) == ["ExpandCollapsePattern.Expand", "ExpandCollapsePattern.Collapse"]
    assert result.ok is False


def test_an_option_without_selection_item_is_refused_and_collapsed(fast_clock: Any) -> None:
    combo = Combo()
    item = OptionItem(name="Large", enabled=True, selectable=False, handle=Item(combo, "Large"))
    result = _executor(_source(combo, item)).execute(_choose(), _snapshot(combo))
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.evidence["detail"] == "option_unselectable"
    assert combo.calls == ["expand", "collapse"]


def test_a_refusal_whose_collapse_never_confirms_is_left_expanded(fast_clock: Any) -> None:
    combo = Combo(collapse_never=True)
    result = _executor(_source(combo, *_items(combo, "Small"))).execute(_choose(), _snapshot(combo))
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.retry_safe is False
    assert result.evidence["detail"] == "left_expanded"
    assert result.evidence["cause"] == "no_match"
    assert combo.calls == ["expand", "collapse"]


def test_the_aria_shape_is_no_options_with_no_collapse(fast_clock: Any) -> None:
    # W4 v2 verdict: Expand has no effect, 0 items; Collapse would throw 0x80131509 there
    combo = Combo(expand_has_effect=False)
    result = _executor(_source(combo, *_items(combo, "Large"))).execute(_choose(), _snapshot(combo))
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.retry_safe is False
    assert result.evidence["detail"] == "no_options"
    assert combo.calls == ["expand"]


def test_the_option_poll_is_bounded_by_the_deadline(fast_clock: Any) -> None:
    combo = Combo()
    options = _source(combo, *_items(combo, "Large"), appear_after=10_000)
    result = _executor(options).execute(_choose(), _snapshot(combo))
    assert result.evidence["detail"] == "no_match"
    assert options.reads <= 101
    assert fast_clock["t"] <= 1.0 + 1.0 + 0.4 + 1e-6  # options, collapse, nothing else


# ---- after Select --------------------------------------------------------------------------------


def test_the_collapse_is_polled_after_select(fast_clock: Any) -> None:
    combo = Combo(collapse_reads=3)
    result = _executor(_source(combo, *_items(combo, "Large"))).execute(_choose(), _snapshot(combo))
    assert result.delivery is Delivery.DELIVERED_VERIFIED, result.to_dict()


def test_a_collapse_that_never_confirms_after_select_is_left_expanded(fast_clock: Any) -> None:
    combo = Combo(collapse_never=True)
    result = _executor(_source(combo, *_items(combo, "Large"))).execute(_choose(), _snapshot(combo))
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.evidence["detail"] == "left_expanded"
    assert combo.calls == ["expand", "select:Large", "collapse"]


def test_a_com_error_on_collapse_after_select_is_uncertain_with_the_hresult(
    fast_clock: Any,
) -> None:
    combo = Combo(collapse_error=com_error(E_FAIL))
    result = _executor(_source(combo, *_items(combo, "Large"))).execute(_choose(), _snapshot(combo))
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.code is ErrorCode.VERIFY_FAILED
    assert result.evidence["hresult"] == "0x80004005"
    assert result.evidence["phase"] == "call"


def test_a_com_error_on_expand_is_uncertain_with_the_hresult(fast_clock: Any) -> None:
    combo = Combo(expand_error=com_error(0x80131509))
    result = _executor(_source(combo, *_items(combo, "Large"))).execute(_choose(), _snapshot(combo))
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.evidence["hresult"] == "0x80131509"
    assert combo.calls == ["expand"]


def test_a_value_that_never_reads_the_option_is_unchanged_for_t1(fast_clock: Any) -> None:
    combo = Combo()
    item = OptionItem(
        name="Large", enabled=True, selectable=True, handle=Item(combo, "Large", value="Small")
    )
    result = _executor(_source(combo, item)).execute(_choose(), _snapshot(combo))
    assert result.delivery is Delivery.DELIVERED_UNVERIFIED
    assert result.code is ErrorCode.VERIFY_TIMEOUT
    assert result.evidence["detail"] == "unchanged"
    assert combo.calls == ["expand", "select:Large", "collapse"]


# ---- the blocker after Expand (W4 v1 verdict item 3, W4-08) --------------------------------------


def test_the_blocker_after_expand_sends_nothing_more_and_leaves_it_expanded(
    fast_clock: Any,
) -> None:
    asked: list[int] = []

    def blocker() -> str | None:
        asked.append(1)
        return None if len(asked) == 1 else "killed"

    combo = Combo()
    result = _executor(_source(combo, *_items(combo, "Large")), blocker).execute(
        _choose(), _snapshot(combo)
    )
    assert combo.calls == ["expand"]
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "left_expanded"
    assert result.evidence["blocker"] == "killed"


def test_the_blocker_before_expand_sends_nothing(fast_clock: Any) -> None:
    combo = Combo()
    result = _executor(_source(combo, *_items(combo, "Large")), lambda: "killed").execute(
        _choose(), _snapshot(combo)
    )
    assert combo.calls == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.GATE_BLOCKED


# ---- the action ----------------------------------------------------------------------------------


def test_choose_needs_an_option_and_no_other_verb_takes_one() -> None:
    with pytest.raises(ValueError, match="option"):
        Action(verb="choose", element_id=1)
    with pytest.raises(ValueError, match="option"):
        Action(verb="choose", element_id=1, option="   ")
    with pytest.raises(ValueError, match="option"):
        Action(verb="click", element_id=1, option="Large")
    with pytest.raises(ValueError, match="text"):
        Action(verb="choose", element_id=1, option="Large", text="Large")


def test_an_option_with_a_control_or_format_character_is_refused() -> None:
    for bad in ("La" + chr(0x7) + "rge", "La" + chr(0x200B) + "rge"):
        with pytest.raises(ValueError, match="option"):
            Action(verb="choose", element_id=1, option=bad)


def test_the_option_never_reaches_a_repr_or_the_result() -> None:
    action = Action(verb="choose", element_id=1, option="Secret plan")
    assert "Secret" not in repr(action)
    item = OptionItem(name="Secret plan", enabled=True, selectable=True, handle=None)
    assert "Secret" not in repr(item)
