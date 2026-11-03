"""Verb templates and the minimum predicate strength per risk tier (design 5.10 [v0.3]): the
template is filled by code from the step's parameters, never by the model (5.4 [v0.5])."""

from __future__ import annotations

import pytest

from steerai.verify.predicates import (
    All,
    AnyOf,
    Appeared,
    CountDelta,
    Disappeared,
    Focused,
    State,
    TitleMatches,
    ValueEquals,
    WindowExists,
    Within,
)
from steerai.verify.strength import Params, strength, strengthen, template_for

CONVERSATION = Within(landmark="Conversation")


def test_type_template_is_the_value_of_the_target() -> None:
    predicate = template_for("type", Params(target_id=12, text="hello there"))
    assert predicate == ValueEquals(target_id=12, text="hello there")


def test_send_template_needs_the_content_in_scope_and_an_empty_composer() -> None:
    predicate = template_for(
        "send", Params(target_id=13, text="hello there", composer_id=12, scope=CONVERSATION)
    )
    assert isinstance(predicate, All)
    first = predicate.parts[0]
    assert isinstance(first, Appeared)
    assert first.name_pattern == "hello there"
    assert first.excluding_anchor == 12
    assert first.within == CONVERSATION


def test_send_template_role_comes_from_the_params() -> None:
    predicate = template_for(
        "send",
        Params(
            target_id=13, text="hi", composer_id=12, scope=CONVERSATION, content_role="ListItem"
        ),
    )
    assert isinstance(predicate, All)
    assert isinstance(predicate.parts[0], Appeared)
    assert predicate.parts[0].role == "ListItem"


def test_delete_template_is_disappeared_and_count_minus_one() -> None:
    predicate = template_for(
        "delete",
        Params(
            target_id=3,
            target_role="ListItem",
            target_name="Amir · hi bro · 2h",
            scope=Within(landmark="Chats"),
        ),
    )
    assert predicate == All(
        (
            Disappeared(
                role="ListItem", name_pattern="Amir · hi bro · 2h", within=Within(landmark="Chats")
            ),
            CountDelta(role="ListItem", delta=-1, within=Within(landmark="Chats")),
        )
    )


def test_open_template_is_a_new_window() -> None:
    assert template_for("open", Params(window_name="Save As")) == WindowExists(
        name_pattern="Save As"
    )


def test_click_has_no_template_the_planner_supplies_the_effect() -> None:
    with pytest.raises(ValueError, match="click"):
        template_for("click", Params(target_id=1))


def test_templates_refuse_missing_parameters() -> None:
    with pytest.raises(ValueError, match="text"):
        template_for("type", Params(target_id=1))
    with pytest.raises(ValueError, match="composer"):
        template_for("send", Params(target_id=1, text="x", scope=CONVERSATION))
    with pytest.raises(ValueError, match="unknown verb"):
        template_for("teleport", Params(target_id=1))


# ---- strength ----------------------------------------------------------------------------------


def test_focused_alone_is_always_weak() -> None:
    for tier in ("R0", "R1", "R2", "R3"):
        assert strength(Focused(target_id=1), tier, Params(target_id=1)).ok is False


def test_r0_and_r1_accept_any_template_predicate() -> None:
    assert strength(State(target_id=1, wanted="on"), "R1", Params(target_id=1)).ok is True
    assert strength(TitleMatches(pattern="Inbox"), "R0", Params()).ok is True
    assert (
        strength(ValueEquals(target_id=1, text="x"), "R1", Params(target_id=1, text="x")).ok is True
    )


def test_r2_needs_a_content_specific_appeared_or_disappeared() -> None:
    params = Params(target_id=13, text="hello there", composer_id=12, scope=CONVERSATION)
    good = template_for("send", params)
    assert strength(good, "R2", params).ok is True
    weak = strength(ValueEquals(target_id=12, text="hello there"), "R2", params)
    assert weak.ok is False
    assert weak.reason == "no_structural_content_predicate"


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        ("ok", "pattern_too_short"),
        ("Amir Khan", "pattern_is_the_recipient"),
        ("Conversation", "pattern_is_the_scope"),
        ("Amir Khan - Chat", "pattern_is_the_window_title"),
        ("tell Amir hello there", "pattern_is_the_goal_text"),
        ("12:30", "pattern_is_time_like"),
        ("Today", "pattern_is_date_like"),
        ("something else", "pattern_not_a_parameter"),
    ],
)
def test_r2_rejects_gamed_patterns(pattern: str, reason: str) -> None:
    params = Params(
        target_id=13,
        text="hello there",
        composer_id=12,
        scope=CONVERSATION,
        recipient="Amir Khan",
        window_title="Amir Khan - Chat",
        goal_text="tell Amir hello there",
    )
    verdict = strength(
        Appeared(role="Text", name_pattern=pattern, within=CONVERSATION), "R3", params
    )
    assert verdict.ok is False
    assert verdict.reason == reason


def test_r2_accepts_a_pattern_equal_to_a_content_parameter() -> None:
    params = Params(
        target_id=13, text="hello there", composer_id=12, scope=CONVERSATION, recipient="Amir"
    )
    verdict = strength(
        Appeared(role="Text", name_pattern="hello there", within=CONVERSATION), "R2", params
    )
    assert verdict.ok is True
    file_params = Params(target_id=3, file_name="invoice-2026.pdf")
    verdict = strength(
        Disappeared(role="ListItem", name_pattern="invoice-2026.pdf"), "R3", file_params
    )
    assert verdict.ok is True


def test_conjunction_is_as_strong_as_its_strongest_structural_part() -> None:
    params = Params(target_id=13, text="hello there", composer_id=12, scope=CONVERSATION)
    weak = All((ValueEquals(target_id=12, text=""), Focused(target_id=13)))
    assert strength(weak, "R2", params).ok is False
    mixed = All(
        (
            Appeared(role="Text", name_pattern="hello there", within=CONVERSATION),
            Focused(target_id=13),
        )
    )
    assert strength(mixed, "R2", params).ok is True
    either = AnyOf((Appeared(role="Text", name_pattern="hello there"), Focused(target_id=13)))
    assert strength(either, "R2", params).ok is False
    assert strength(either, "R2", params).reason == "any_of_has_a_weak_branch"


def test_strengthen_replaces_a_weak_predicate_with_the_verb_template() -> None:
    params = Params(target_id=13, text="hello there", composer_id=12, scope=CONVERSATION)
    strengthened = strengthen(Focused(target_id=13), "send", "R2", params)
    assert strengthened == template_for("send", params)
    assert strength(strengthened, "R2", params).ok is True


def test_strengthen_keeps_a_predicate_that_is_already_strong() -> None:
    params = Params(target_id=12, text="hello there")
    strong = ValueEquals(target_id=12, text="hello there")
    assert strengthen(strong, "type", "R1", params) is strong


def test_strengthen_returns_none_when_the_template_is_weak_too() -> None:
    params = Params(target_id=13, text="ok", composer_id=12, scope=CONVERSATION)
    assert strengthen(Focused(target_id=13), "send", "R2", params) is None
