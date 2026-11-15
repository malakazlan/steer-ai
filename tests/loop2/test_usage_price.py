"""[V2.40] Q3, [V2.41] (1), [V2.45], adapter slice 2: usage and price by token category. The API
reports `input_tokens`, `output_tokens`, the cache writes (`cache_creation_input_tokens`, split by
lifetime in `cache_creation`) and `cache_read_input_tokens`; `input_tokens` excludes the cache
categories. The token budget counts all five; each cache-write lifetime has its own rate, and a
category used with no rate makes the dollar figure "not configured", never 0.0."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from steerai.loop2.loop import TaskOutcome, TokenUsage
from steerai.loop2.runner import run_task
from steerai.loop2.task import Budgets, TokenPrice
from tests.loop2.world_fakes import GO, PRICE_READ_ON, PRICE_URL, World, act, decision, task

SOURCE = PRICE_URL
READ_ON = PRICE_READ_ON
USED = TokenUsage(
    input_tokens=100,
    output_tokens=10,
    cache_write_5m=1_000,
    cache_write_1h=2_000,
    cache_read=5_000,
)
FULL = TokenPrice(
    usd_per_input_token=4e-6,
    usd_per_output_token=20e-6,
    usd_per_cache_write_5m_token=5e-6,
    usd_per_cache_write_1h_token=8e-6,
    usd_per_cache_read_token=0.4e-6,
    source=SOURCE,
    read_on=READ_ON,
)
CACHE_RATES = (
    "usd_per_cache_write_5m_token",
    "usd_per_cache_write_1h_token",
    "usd_per_cache_read_token",
)


def _price(**rates: float) -> TokenPrice:
    base: dict[str, float] = {"usd_per_input_token": 4e-6, "usd_per_output_token": 20e-6}
    return TokenPrice(**(base | rates), source=SOURCE, read_on=READ_ON)


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


def test_the_token_count_takes_every_category() -> None:
    outcome = _run(USED)
    assert outcome.tokens == 2 * 8_110


def test_the_cost_is_each_category_at_its_own_rate() -> None:
    per_call = 100 * 4e-6 + 10 * 20e-6 + 1_000 * 5e-6 + 2_000 * 8e-6 + 5_000 * 0.4e-6
    outcome = _run(USED)
    assert outcome.cost_usd == pytest.approx(2 * per_call)


@pytest.mark.parametrize("missing", CACHE_RATES)
def test_a_used_category_with_no_rate_makes_the_cost_not_configured(missing: str) -> None:
    rates = {name: 5e-6 for name in CACHE_RATES if name != missing}
    outcome = _run(USED, _price(**rates))
    assert outcome.cost_usd is None


def test_a_five_minute_rate_never_prices_a_one_hour_write() -> None:
    """[V2.45]: one write rate for both lifetimes looks enforced and is wrong once they mix."""
    hour_only = TokenUsage(input_tokens=1, output_tokens=1, cache_write_1h=1_000)
    price = _price(usd_per_cache_write_5m_token=5e-6)
    assert _run(hour_only, price).cost_usd is None


def test_an_unused_category_needs_no_rate() -> None:
    plain = TokenUsage(input_tokens=100, output_tokens=10)
    outcome = _run(plain, _price())
    assert outcome.cost_usd == pytest.approx(2 * (100 * 4e-6 + 10 * 20e-6))


def test_a_missing_usage_marks_the_outcome_incomplete() -> None:
    """[V2.41] (1): a call whose usage was not reported is counted, never as zero: the token
    budget trips and the outcome says the usage is incomplete."""
    outcome = _run(None)
    assert outcome.usage_incomplete is True
    assert math.isnan(outcome.tokens)


def test_a_complete_usage_is_not_marked_incomplete() -> None:
    assert _run(USED).usage_incomplete is False


@pytest.mark.parametrize("field", ["cache_write_5m", "cache_write_1h", "cache_read"])
def test_a_negative_cache_count_is_an_invalid_usage(field: str) -> None:
    bad = TokenUsage(input_tokens=1, output_tokens=1, **{field: -1})
    outcome = _run(bad)
    assert math.isnan(outcome.tokens)
    assert outcome.usage_incomplete is True


@pytest.mark.parametrize("name", CACHE_RATES)
@pytest.mark.parametrize("bad", [0.0, -1e-6, math.nan, math.inf])
def test_a_cache_rate_must_be_finite_and_positive_when_given(name: str, bad: float) -> None:
    with pytest.raises(ValueError, match="price"):
        _price(**{name: bad})


def test_the_outcome_carries_the_summed_usage_by_category() -> None:
    """[V2.45]: stage 1 runs with caching off and asserts every cache counter is zero."""
    assert _run(USED).usage == TokenUsage(
        input_tokens=200,
        output_tokens=20,
        cache_write_5m=2_000,
        cache_write_1h=4_000,
        cache_read=10_000,
    )
    plain = _run(TokenUsage(input_tokens=7, output_tokens=3)).usage
    assert (plain.cache_write_5m, plain.cache_write_1h, plain.cache_read) == (0, 0, 0)


@pytest.mark.parametrize(
    "source", ["", "platform.claude.com/docs", "http://platform.claude.com/docs", "https://a b"]
)
def test_a_price_names_the_https_page_it_was_read_from(source: str) -> None:
    with pytest.raises(ValueError, match="source"):
        TokenPrice(
            usd_per_input_token=1e-6, usd_per_output_token=1e-6, source=source, read_on=READ_ON
        )


@pytest.mark.parametrize("read_on", [datetime(2026, 11, 15, 9, 0, tzinfo=UTC), "2026-11-15", None])
def test_a_price_records_the_day_it_was_read(read_on: object) -> None:
    with pytest.raises(ValueError, match="read_on"):
        TokenPrice(
            usd_per_input_token=1e-6,
            usd_per_output_token=1e-6,
            source=SOURCE,
            read_on=read_on,  # type: ignore[arg-type]  # the check under test
        )
