"""§7.1 code-only claims, step 3 ([V2.29] Q2): `state` and `value` are decided by one live read
of the cited element, three-valued (an unread read rejects with its own token, never "false");
neither reads a field whose secure flag is set or unread. Pure: the readers are fakes."""

from __future__ import annotations

import dataclasses

import pytest

from steerai.loop2.decision import Claim
from steerai.loop2.task import GoalSpan, Subgoal
from steerai.loop2.verify import Readers, check_claim
from steerai.world.effects import Observed, ValueFact
from steerai.world.elements import Element
from tests.loop2.world_fakes import (
    BAR,
    DOC,
    FIELD,
    GOAL,
    PAGE,
    PASSWORD,
    element,
    observed,
    snapshot,
)

BOX = element(30, "CheckBox", "Gift wrap", (9, 30), 1)
SHOWN = observed([*PAGE, BOX])
START = GOAL.index("red shoes")
SUB = Subgoal(
    id="s1",
    desc="search",
    closes="page",
    params={"text": GoalSpan.of(GOAL, START, START + len("red shoes"))},
)
TYPED = ValueFact(filled=True, digest="d:red shoes")


def readers(
    state: bool | None = True, value: ValueFact | None = TYPED, reads: list[int] | None = None
) -> Readers:
    def read_state(e: Element, wanted: str) -> bool | None:
        if reads is not None:
            reads.append(e.id)
        return state

    def read_value(e: Element) -> ValueFact | None:
        if reads is not None:
            reads.append(e.id)
        return value

    return Readers(
        read_state=read_state,
        read_value=read_value,
        fact_of=lambda text: ValueFact(filled=bool(text), digest=f"d:{text}"),
    )


def check(claim: Claim, seen: Observed = SHOWN, **kw: object) -> tuple[str, ...]:
    return check_claim(claim, SUB, seen, seen, readers=readers(**kw))  # type: ignore[arg-type]  # test kwargs


def state(*ids: int, wanted: str = "on") -> Claim:
    return Claim(subgoal="s1", kind="state", evidence=list(ids), wanted=wanted)  # type: ignore[arg-type]  # a closed literal


def value(*ids: int) -> Claim:
    return Claim(subgoal="s1", kind="value", evidence=list(ids), param="text")


# ---- state -----------------------------------------------------------------------------------


def test_a_state_read_live_as_wanted_is_proven() -> None:
    assert check(state(BOX.id)) == ()


def test_a_state_read_otherwise_is_a_mismatch() -> None:
    assert check(state(BOX.id), state=False) == ("state_mismatch",)


def test_an_unread_state_rejects_with_its_own_token_never_false() -> None:
    assert check(state(BOX.id), state=None) == ("state_unread",)


@pytest.mark.parametrize(
    ("ids", "token"), [((), "evidence_missing"), ((BOX.id, FIELD.id), "too_much_evidence")]
)
def test_a_state_claim_cites_exactly_one_element(ids: tuple[int, ...], token: str) -> None:
    assert check(state(*ids)) == (token,)


def test_the_address_bar_is_never_evidence() -> None:
    seen = dataclasses.replace(SHOWN, snapshot=snapshot([*PAGE, BOX]))
    tokens = check_claim(
        state(BAR.id), SUB, seen, seen, readers=readers(), scope_document=DOC.runtime_id
    )
    assert tokens == ("outside_scope",)


# ---- value -----------------------------------------------------------------------------------


def test_a_field_holding_the_param_text_is_proven() -> None:
    assert check(value(FIELD.id)) == ()


def test_a_field_holding_other_text_is_a_mismatch() -> None:
    assert check(value(FIELD.id), value=ValueFact(filled=True, digest="d:blue")) == (
        "value_mismatch",
    )


def test_an_unread_value_rejects_with_its_own_token_never_false() -> None:
    assert check(value(FIELD.id), value=None) == ("value_unread",)


def test_a_secure_field_is_never_read_for_a_value_claim() -> None:
    reads: list[int] = []
    tokens = check_claim(value(PASSWORD.id), SUB, SHOWN, SHOWN, readers=readers(reads=reads))
    assert (tokens, reads) == (("secure_field",), [])


def test_a_field_with_an_unread_secure_flag_is_never_read() -> None:
    unread = dataclasses.replace(
        SHOWN, snapshot=dataclasses.replace(SHOWN.snapshot, unread_secure=frozenset({FIELD.id}))
    )
    reads: list[int] = []
    tokens = check_claim(value(FIELD.id), SUB, unread, unread, readers=readers(reads=reads))
    assert (tokens, reads) == (("secure_unread",), [])
