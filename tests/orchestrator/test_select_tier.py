"""The tier of a `select` (W3 verdict ruling (a), W3-03, W3-02): the consent class reads a
RadioButton's name; a RadioButton's nearest Group ancestor's name goes through the deny, control
and consent patterns; billing words and prices raise a RadioButton or a CheckBox to R2. The loop
passes the group name from the resolved element's parent, so the gate decides and the admission
re-runs at the element-aware tier."""

from __future__ import annotations

from typing import Any

import pytest

from steerai.orchestrator.contracts import Plan, Step, Subgoal, Target
from steerai.orchestrator.gate import Gate, deny_tier, effective_risk
from steerai.verify.predicates import State, TitleMatches
from tests.orchestrator.fakes import BASE_ROWS, Row
from tests.orchestrator.test_loop import Harness
from tests.orchestrator.test_state_verbs_loop import Boxes

PAY_GROUP = "I agree to pay the monthly fee"
SHARE_GROUP = "I consent to data sharing"


def _select(name: str, role: str = "RadioButton") -> Step:
    return Step(id="s1", subgoal="g1", verb="select", target=Target(role=role, name=name))


def _check(name: str) -> Step:
    return Step(id="s1", subgoal="g1", verb="check", target=Target(role="CheckBox", name=name))


# ---- the group name (W3-03) ----------------------------------------------------------------------


def test_yes_under_a_group_that_agrees_to_pay_reaches_r3() -> None:
    assert deny_tier(_select("Yes"), group_name=PAY_GROUP) == "R3"
    assert effective_risk(_select("Yes"), group_name=PAY_GROUP) == "R3"


@pytest.mark.parametrize("name", ["Allow", "Deny"])
def test_an_option_under_a_consent_group_reaches_r2(name: str) -> None:
    assert effective_risk(_select(name), group_name=SHARE_GROUP) == "R2"


def test_a_control_word_in_the_group_name_raises_the_radio() -> None:
    assert effective_risk(_select("Yes"), group_name="Subscribe to the newsletter") == "R2"


@pytest.mark.parametrize("group", [None, "", "Delivery speed"])
def test_a_radio_with_no_named_raising_group_stays_at_its_own_tier(group: str | None) -> None:
    assert deny_tier(_select("Deny"), group_name=group) == "R0"
    assert effective_risk(_select("Yes"), group_name=group) == "R1"


def test_a_tabs_group_name_is_never_read() -> None:
    """TabItem gets the deny patterns on its own name only (verdict ruling (a))."""
    assert effective_risk(_select("Yes", "TabItem"), group_name=PAY_GROUP) == "R1"
    assert effective_risk(_select("Delete", "TabItem")) == "R3"


def test_a_checkboxs_group_name_is_read_for_billing_only() -> None:
    """W3-03 names a RadioButton; W3-02 reads a CheckBox's group name for billing."""
    assert effective_risk(_check("Email receipts"), group_name=PAY_GROUP) == "R1"
    assert effective_risk(_check("Email receipts"), group_name="Billing emails") == "R2"


# ---- the consent class on a radio (ruling (a)) ---------------------------------------------------


@pytest.mark.parametrize("name", ["I agree", "I do not agree", "I consent", "I certify this"])
def test_the_consent_class_raises_a_radio_option(name: str) -> None:
    assert effective_risk(_select(name)) == "R2"


def test_the_consent_class_never_raises_a_tab() -> None:
    assert effective_risk(_select("I agree", "TabItem")) == "R1"


# ---- billing (W3-02) -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Annual ($120/yr)",
        "$120",
        "$ 120",
        "120$",
        "120€",
        "120 €",
        "€9.99",
        "£5 a week",
        "¥500",
        "₹99",
        "\uff04120",  # a full-width dollar sign folds to $ under NFKC
        "\ufe69120",  # a small dollar sign folds to $ under NFKC
        "\uff04\uff11\uff12\uff10",  # full-width digits fold too
        "10/mo",
        "Pro / yr",
        "Billed per month",
        "Billed per year",
        "Monthly subscription",
        "Billing address same as shipping",
    ],
)
@pytest.mark.parametrize("role", ["RadioButton", "CheckBox"])
def test_billing_raises_a_radio_or_a_checkbox_to_r2(name: str, role: str) -> None:
    step = _select(name) if role == "RadioButton" else _check(name)
    assert deny_tier(step) == "R2", name


@pytest.mark.parametrize(
    "name",
    [
        "Monthly",
        "Annual",
        "Plan your trip",
        "Basic plan",
        "120 items",
        "$ off",
        "Mode",
        "Year in review",
        "Momentum",
        "/ yearly digest",
    ],
)
def test_no_price_and_no_billing_word_stays_r1(name: str) -> None:
    assert effective_risk(_select(name)) == "R1", name


def test_a_billing_group_raises_its_options() -> None:
    assert effective_risk(_select("Monthly"), group_name="Billing period") == "R2"
    assert effective_risk(_select("Annual"), group_name="Pay $120 a year") == "R3"


def test_billing_never_raises_a_tab_or_a_button() -> None:
    assert effective_risk(_select("$120", "TabItem")) == "R1"
    button = Step(id="s1", subgoal="g1", verb="click", target=Target(role="Button", name="$120"))
    assert deny_tier(button) == "R0"


# ---- the gate decides at the element-aware tier --------------------------------------------------


def test_the_gate_asks_at_the_group_raised_tier() -> None:
    from tests.orchestrator.test_state_verbs import _Confirmer  # noqa: PLC0415

    confirmer = _Confirmer()
    gate = Gate(confirmer=confirmer, own_pid=1, task_id="t")
    result = gate.decide(
        _select("Yes"), action_key="k", expect=None, target_pid=2, target_id=5, group_name=PAY_GROUP
    )
    assert result.outcome == "confirm", result.evidence
    assert result.expect == State(target_id=5, wanted="selected")
    assert [a.risk for a in confirmer.asked] == ["R3"]


# ---- the loop passes the resolved element's group name -------------------------------------------

FORM: Row = (11, "Group", PAY_GROUP, (42, 50), None)
YES: Row = (12, "RadioButton", "Yes", (42, 51), 11)
NO: Row = (13, "RadioButton", "No", (42, 52), 11)
PANE: Row = (14, "Pane", PAY_GROUP, (42, 53), None)
SURE: Row = (15, "RadioButton", "Sure", (42, 54), 14)


def _selects(boxes: Boxes) -> Any:
    """Select clears the element's peers under the same parent, then selects it."""

    def on_execute(_ex: Any, action: Any) -> None:
        row = next(r for r in boxes.world.rows if r[0] == action.element_id)
        for peer in boxes.world.rows:
            if peer[4] == row[4]:
                boxes.selected[peer[3]] = False
        boxes.selected[row[3]] = True

    return on_execute


def _harness(*rows: Row) -> tuple[Harness, Boxes]:
    h = Harness()
    h.world.rows = [*BASE_ROWS, *rows]
    boxes = Boxes(h.world, selected={r[3]: False for r in rows})
    h.session._live = boxes
    h.semantic.on_execute = _selects(boxes)
    return h, boxes


def test_the_loop_asks_for_yes_under_a_group_that_agrees_to_pay() -> None:
    h, boxes = _harness(FORM, YES, NO)
    result = h.session.run_step(_select("Yes"))
    assert result.ok is True, result.to_dict()
    assert result.evidence["risk"] == "R3"
    assert [a.risk for a in h.confirmer.asked] == ["R3"]
    assert h.confirmer.asked[0].expect == {
        "kind": "State",
        "target_id": YES[0],
        "wanted": "selected",
    }
    assert boxes.selected[YES[3]] is True
    assert "s1" in h.ledger.records, "the raised step is written ahead like any R3 step"


def test_the_loop_reads_a_group_parent_only() -> None:
    """A Pane of the same name is no group: the plain-div question is the written gap."""
    h, _boxes = _harness(PANE, SURE)
    result = h.session.run_step(_select("Sure"))
    assert result.ok is True, result.to_dict()
    assert result.evidence["risk"] == "R1"
    assert h.confirmer.asked == []


def test_a_group_raised_step_never_starts_on_an_unmet_earlier_subgoal() -> None:
    """#190 review item 1: the [v2.36] precondition rule judged the plan's tier, so "Yes"
    under "I agree to pay" (R3 only through its group) started while an earlier subgoal was
    unticked. The settled tier now runs the same rule: stop, nothing sent, no ask."""
    h, boxes = _harness(FORM, YES, NO)
    first = Step(
        id="s0",
        subgoal="g0",
        verb="click",
        target=Target(role="Button", name="Chat options"),
        expect=TitleMatches(pattern="never shown"),
    )
    plan = Plan(
        goal="open the options, then agree to pay",
        subgoals=(
            Subgoal(id="g0", desc="options open", condition=TitleMatches(pattern="never shown")),
            Subgoal(id="g1", desc="agreed", condition=TitleMatches(pattern="Done")),
        ),
        steps=(first, _select("Yes")),
    )
    task = h.session.run_task(plan)
    assert task.status == "stopped", task
    assert task.evidence["detail"] == "precondition_unmet"
    assert h.confirmer.asked == []
    assert ("select", YES[0], None) not in h.semantic.calls
    assert boxes.selected[YES[3]] is False
