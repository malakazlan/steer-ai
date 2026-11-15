"""[V2.40] Q3, [V2.41] (1), adapter slice 2: usage and price in four categories. The API reports
`input_tokens`, `output_tokens`, `cache_creation_input_tokens` and `cache_read_input_tokens`
(`input_tokens` excludes the cache categories); the token budget counts all four, and a
category used with no rate makes the dollar figure "not configured", never 0.0."""

from __future__ import annotations

import math

import pytest

from steerai.loop2.loop import TaskOutcome, TokenUsage
from steerai.loop2.runner import run_task
from steerai.loop2.task import Budgets, TokenPrice
from tests.loop2.world_fakes import GO, World, act, decision, task

USED = TokenUsage(
    input_tokens=100,
    output_tokens=10,
    cache_creation_input_tokens=1_000,
    cache_read_input_tokens=5_000,
)
FULL = TokenPrice(
    usd_per_input_token=4e-6,
    usd_per_output_token=20e-6,
    usd_per_cache_write_token=5e-6,
    usd_per_cache_read_token=0.4e-6,
)


def _budgets(price: TokenPrice | None) -> Budgets:
    return Budgets(
        max_actions=20,
        max_model_calls=10,
        max_tokens=1_000_000,
        max_cost_usd=10.0,
        max_wall_s=60.0,
        price=price,
    )


def _run(usage: TokenUsage | None, price: TokenPrice | None = FULL) -> TaskOutcome:
    world = World([decision(act("click", target=GO.id), first=True)])
    world.usage = usage
    return run_task(task(_budgets(price)), world.ports())


def test_the_token_count_takes_all_four_categories() -> None:
    outcome = _run(USED)
    assert outcome.tokens == 2 * 6_110


def test_the_cost_is_each_category_at_its_own_rate() -> None:
    per_call = 100 * 4e-6 + 10 * 20e-6 + 1_000 * 5e-6 + 5_000 * 0.4e-6
    outcome = _run(USED)
    assert outcome.cost_usd == pytest.approx(2 * per_call)


def test_a_used_category_with_no_rate_makes_the_cost_not_configured() -> None:
    no_cache_read = TokenPrice(
        usd_per_input_token=4e-6, usd_per_output_token=20e-6, usd_per_cache_write_token=5e-6
    )
    outcome = _run(USED, no_cache_read)
    assert outcome.cost_usd is None


def test_an_unused_category_needs_no_rate() -> None:
    plain = TokenUsage(input_tokens=100, output_tokens=10)
    price = TokenPrice(usd_per_input_token=4e-6, usd_per_output_token=20e-6)
    outcome = _run(plain, price)
    assert outcome.cost_usd == pytest.approx(2 * (100 * 4e-6 + 10 * 20e-6))


def test_a_missing_usage_marks_the_outcome_incomplete() -> None:
    """[V2.41] (1): a call whose usage was not reported is counted, never as zero: the token
    budget trips and the outcome says the usage is incomplete."""
    outcome = _run(None)
    assert outcome.usage_incomplete is True
    assert math.isnan(outcome.tokens)


def test_a_complete_usage_is_not_marked_incomplete() -> None:
    assert _run(USED).usage_incomplete is False


def test_a_negative_cache_count_is_an_invalid_usage() -> None:
    bad = TokenUsage(input_tokens=1, output_tokens=1, cache_read_input_tokens=-1)
    outcome = _run(bad)
    assert math.isnan(outcome.tokens)
    assert outcome.usage_incomplete is True


@pytest.mark.parametrize("bad", [0.0, -1e-6, math.nan, math.inf])
def test_a_cache_rate_must_be_finite_and_positive_when_given(bad: float) -> None:
    with pytest.raises(ValueError, match="price"):
        TokenPrice(
            usd_per_input_token=1e-6, usd_per_output_token=1e-6, usd_per_cache_read_token=bad
        )
