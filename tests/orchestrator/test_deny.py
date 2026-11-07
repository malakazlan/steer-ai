"""Deny patterns as 5.13 1a states them (RN-03/04/25) and the ask's target identity (RN-06)."""

from __future__ import annotations

import pytest

from steerai.orchestrator.contracts import Step, Target
from steerai.orchestrator.gate import DENY_PATTERNS, Ask, Gate, effective_risk
from tests.orchestrator.fakes import FakeConfirmer


def _click(name: str, automation_id: str = "", risk: str = "R0") -> Step:
    return Step(
        id="s",
        subgoal="g1",
        verb="click",
        target=Target(role="Button", name=name, automation_id=automation_id),
        risk=risk,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("name", "tier"),
    [
        ("Confirm", "R2"),
        ("Sign out", "R2"),
        ("Sign Out of all devices", "R2"),
        ("Install", "R3"),
        ("Uninstall", "R3"),
        ("Format", "R3"),
        ("Reset", "R3"),
    ],
)
def test_the_design_docs_deny_words_raise_the_tier(name: str, tier: str) -> None:
    assert effective_risk(_click(name)) == tier


def test_every_deny_pattern_of_the_design_doc_is_listed() -> None:
    for word in (
        "delete",
        "remove",
        "pay",
        "purchase",
        "send",
        "submit",
        "confirm",
        "install",
        "uninstall",
        "format",
        "reset",
        "sign out",
    ):
        assert word in DENY_PATTERNS, word


@pytest.mark.parametrize(
    "name",
    ["Dele\u200bte chat", "S\u200dend", "\u202eDelete chat\u202c", "Sign\u200bout"],
)
def test_format_characters_never_hide_a_deny_word(name: str) -> None:
    assert effective_risk(_click(name)) in ("R2", "R3")


@pytest.mark.parametrize("name", ["Delete_all", "Send2", "SEND", "Resend"])
def test_digits_and_underscores_never_hide_a_word_but_longer_words_are_not_it(name: str) -> None:
    assert effective_risk(_click(name)) == (
        "R0" if name == "Resend" else effective_risk(_click(name))
    )
    assert (effective_risk(_click(name)) != "R0") == (name != "Resend")


@pytest.mark.parametrize(
    "automation_id",
    ["btnDelete", "delete_chat", "DeleteChat", "chat-delete-button", "SEND_NOW"],
)
def test_deny_patterns_also_run_over_the_automation_id(automation_id: str) -> None:
    assert effective_risk(_click("OK", automation_id)) in ("R2", "R3")


def test_a_harmless_automation_id_keeps_the_tier() -> None:
    assert effective_risk(_click("OK", "btnOkDialog")) == "R0"


def test_the_ask_carries_the_target_name_and_automation_id_out_of_its_repr() -> None:
    confirmer = FakeConfirmer(default="no")
    gate = Gate(confirmer=confirmer, own_pid=1, task_id="t")
    step = Step(
        id="d",
        subgoal="g1",
        verb="delete",
        target=Target(role="ListItem", name="Amir Khan", automation_id="row-7"),
        risk="R3",
        params={"scope": "Chat list"},
    )
    gate.decide(step, action_key="k", expect=None, target_pid=2, target_id=3)
    assert len(confirmer.asked) == 1
    ask = confirmer.asked[0]
    assert isinstance(ask, Ask)
    assert ask.target_name == "Amir Khan"
    assert ask.automation_id == "row-7"
    assert "Amir" not in repr(ask)
    assert "row-7" not in repr(ask)


def test_a_resolved_elements_automation_id_raises_never_lowers() -> None:
    """#84: the element the step resolves to brings its own id to the deny matcher."""
    from steerai.orchestrator.contracts import Step, Target  # noqa: PLC0415 - this test only
    from steerai.orchestrator.gate import effective_risk  # noqa: PLC0415

    ok = Step(id="c", subgoal="g", verb="click", target=Target(role="Button", name="OK"), risk="R0")
    assert effective_risk(ok, element_ids=("btnDeleteAccount",)) == "R3"
    assert effective_risk(ok, element_ids=("btnOk", "")) == "R0"
    raised = Step(
        id="c", subgoal="g", verb="delete", target=Target(role="Button", name="OK"), risk="R3"
    )
    assert effective_risk(raised, element_ids=("btnOk",)) == "R3"
