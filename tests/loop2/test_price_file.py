"""[V2.53] (2): the price file `--loop v2` reads before it starts. The model must be the run's,
all five rates finite and positive, the source an https page and the read date a real past day;
the per-task cost cap lives in the same file and is required. An old read date warns, never
refuses."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.loop2.price import MAX_PRICE_FILE_BYTES, STALE_DAYS, PriceFile, load_price

MODEL = "claude-opus-5"
TODAY = date(2026, 11, 15)
RATES = {"input": 5.0, "output": 25.0, "cache_write_5m": 6.25, "cache_write_1h": 10.0,
         "cache_read": 0.5}  # fmt: skip


def _doc(**changes: object) -> dict[str, object]:
    doc: dict[str, object] = {
        "model": MODEL,
        "source": "https://docs.example.com/pricing",
        "read_on": "2026-11-10",
        "usd_per_million_tokens": dict(RATES),
        "max_cost_usd_per_task": 0.5,
    }
    doc.update(changes)
    return doc


def _load(tmp_path: Path, doc: object) -> PriceFile:
    path = tmp_path / "price.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return load_price(path, model=MODEL, today=TODAY)


def _refused(tmp_path: Path, doc: object) -> str:
    with pytest.raises(SteerError) as refused:
        _load(tmp_path, doc)
    assert refused.value.code is ErrorCode.GATE_BLOCKED
    return refused.value.message


def test_a_valid_file_gives_per_token_rates_and_the_cost_cap(tmp_path: Path) -> None:
    loaded = _load(tmp_path, _doc())
    price = loaded.price
    assert price.usd_per_input_token == pytest.approx(5.0e-6)
    assert price.usd_per_output_token == pytest.approx(25.0e-6)
    assert price.usd_per_cache_write_5m_token == pytest.approx(6.25e-6)
    assert price.usd_per_cache_write_1h_token == pytest.approx(10.0e-6)
    assert price.usd_per_cache_read_token == pytest.approx(0.5e-6)
    assert price.source == "https://docs.example.com/pricing"
    assert price.read_on == date(2026, 11, 10)
    assert loaded.max_cost_usd == 0.5
    assert loaded.stale is False


def test_a_missing_file_refuses(tmp_path: Path) -> None:
    with pytest.raises(SteerError) as refused:
        load_price(tmp_path / "absent.json", model=MODEL, today=TODAY)
    assert refused.value.message == "price_missing"


@pytest.mark.parametrize("text", ["{", "[]", '"x"', ""], ids=["broken", "list", "string", "empty"])
def test_a_file_that_is_not_one_json_object_refuses(tmp_path: Path, text: str) -> None:
    path = tmp_path / "price.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SteerError) as refused:
        load_price(path, model=MODEL, today=TODAY)
    assert refused.value.message == "price_unreadable"


def test_a_file_over_the_size_limit_is_never_parsed(tmp_path: Path) -> None:
    path = tmp_path / "price.json"
    valid = json.dumps(_doc()).encode("utf-8")
    path.write_bytes(valid + b" " * (MAX_PRICE_FILE_BYTES + 1 - len(valid)))  # valid, too big
    with pytest.raises(SteerError) as refused:
        load_price(path, model=MODEL, today=TODAY)
    assert refused.value.message == "price_unreadable"


def test_a_price_for_another_model_is_not_a_configured_price(tmp_path: Path) -> None:
    assert _refused(tmp_path, _doc(model="claude-sonnet-5")) == "price_model"


@pytest.mark.parametrize("name", sorted(RATES))
@pytest.mark.parametrize(
    "bad",
    [None, 0, -1.0, math.inf, "5", True],
    ids=["missing", "zero", "neg", "inf", "str", "bool"],
)
def test_every_one_of_the_five_rates_must_be_finite_and_positive(
    tmp_path: Path, name: str, bad: object
) -> None:
    rates: dict[str, object] = dict(RATES)
    if bad is None:
        del rates[name]
    else:
        rates[name] = bad
    assert _refused(tmp_path, _doc(usd_per_million_tokens=rates)) == "price_rate"


def test_a_nan_rate_refuses(tmp_path: Path) -> None:
    path = tmp_path / "price.json"
    text = json.dumps(_doc()).replace('"input": 5.0', '"input": NaN')
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SteerError) as refused:
        load_price(path, model=MODEL, today=TODAY)
    assert refused.value.message == "price_rate"


def test_an_unknown_rate_refuses(tmp_path: Path) -> None:
    assert _refused(tmp_path, _doc(usd_per_million_tokens={**RATES, "batch": 1.0})) == "price_rate"


@pytest.mark.parametrize("cap", [None, 0, -0.5, math.inf, "1", False])
def test_the_per_task_cost_cap_is_required_finite_and_positive(tmp_path: Path, cap: object) -> None:
    doc = _doc()
    if cap is None:
        del doc["max_cost_usd_per_task"]
    else:
        doc["max_cost_usd_per_task"] = cap
    assert _refused(tmp_path, doc) == "price_cost_cap"


@pytest.mark.parametrize("source", ["http://docs.example.com", "docs", "https://a b", 3, None])
def test_the_source_is_an_https_page(tmp_path: Path, source: object) -> None:
    assert _refused(tmp_path, _doc(source=source)) == "price_source"


@pytest.mark.parametrize("read_on", ["2026-13-01", "15/11/2026", "2026-11-16", 20261110, None])
def test_the_read_date_is_a_real_day_not_in_the_future(tmp_path: Path, read_on: object) -> None:
    assert _refused(tmp_path, _doc(read_on=read_on)) == "price_read_on"


def test_an_unknown_key_refuses(tmp_path: Path) -> None:
    assert _refused(tmp_path, _doc(note="x")) == "price_unknown_key"


def test_an_old_read_date_warns_and_still_loads(tmp_path: Path) -> None:
    old = date.fromordinal(TODAY.toordinal() - STALE_DAYS - 1).isoformat()
    edge = date.fromordinal(TODAY.toordinal() - STALE_DAYS).isoformat()
    assert _load(tmp_path, _doc(read_on=old)).stale is True
    assert _load(tmp_path, _doc(read_on=edge)).stale is False


# ---- #361 hunter rows ------------------------------------------------------------------------


def _raw(tmp_path: Path, text: str, *, bom: bool = False) -> PriceFile:
    path = tmp_path / "price.json"
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return load_price(path, model=MODEL, today=TODAY)


def _raw_refused(tmp_path: Path, text: str) -> str:
    with pytest.raises(SteerError) as refused:
        _raw(tmp_path, text)
    return refused.value.message


def test_an_integer_too_large_for_a_float_is_a_bad_rate_not_a_crash(tmp_path: Path) -> None:
    """PRC-01: `math.isfinite(10**400)` raises OverflowError."""
    huge = "1" + "0" * 400
    text = json.dumps(_doc()).replace('"input": 5.0', f'"input": {huge}')
    assert _raw_refused(tmp_path, text) == "price_rate"
    capped = json.dumps(_doc()).replace(
        '"max_cost_usd_per_task": 0.5', f'"max_cost_usd_per_task": {huge}'
    )
    assert _raw_refused(tmp_path, capped) == "price_cost_cap"


def test_a_rate_that_underflows_to_zero_per_token_is_refused(tmp_path: Path) -> None:
    """PRC-02: 5e-324 per million is 0.0 per token, which would silently turn the cost off."""
    assert (
        _refused(tmp_path, _doc(usd_per_million_tokens={**RATES, "output": 5e-324})) == "price_rate"
    )


@pytest.mark.parametrize(
    "source",
    [
        "https://user:pw@docs.example.com/pricing",
        "https://docs.example.com/pricing?key=x",
        "https://docs.example.com/pricing#top",
        "https://docs.example.com/" + "a" * 300,
        "https:///pricing",
    ],
    ids=["userinfo", "query", "fragment", "long", "no-host"],
)
def test_the_source_carries_no_userinfo_query_or_fragment(tmp_path: Path, source: str) -> None:
    """PRC-09: the source goes to the header and the trace; nothing but a plain page."""
    assert _refused(tmp_path, _doc(source=source)) == "price_source"


def test_a_file_saved_with_a_bom_is_read(tmp_path: Path) -> None:
    """PRC-04: Notepad writes UTF-8 with a BOM."""
    assert _raw(tmp_path, json.dumps(_doc()), bom=True).max_cost_usd == 0.5


def test_a_duplicate_key_refuses(tmp_path: Path) -> None:
    """PRC-05: the last of two `model` keys would win silently."""
    text = json.dumps(_doc())[:-1] + ', "max_cost_usd_per_task": 99}'
    assert _raw_refused(tmp_path, text) == "price_duplicate_key"
    nested = json.dumps(_doc()).replace('"input": 5.0', '"input": 5.0, "input": 0.0')
    assert _raw_refused(tmp_path, nested) == "price_duplicate_key"
