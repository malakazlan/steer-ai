"""[V2.53] (2), [V2.45]: the price file `--loop v2` reads before it starts.

```json
{"model": "claude-opus-5", "source": "https://...", "read_on": "2026-11-15",
 "usd_per_million_tokens": {"input": 5.0, "output": 25.0, "cache_write_5m": 6.25,
                            "cache_write_1h": 10.0, "cache_read": 0.5},
 "max_cost_usd_per_task": 0.5}
```

Rates are configuration, never constants: the file names the page they were read from and the
day. The model must be the one the run uses (a price for another model is not a configured
price); all five rates are finite and positive; the per-task cost cap is required, finite and
positive. Anything else refuses to start (`GATE_BLOCKED` with a closed `price_*` token). A read
date older than `STALE_DAYS` only warns: the owner decides when to re-read.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final, TypeGuard
from urllib.parse import urlsplit

from steerai.errors import ErrorCode, SteerError
from steerai.loop2.task import TokenPrice

MAX_PRICE_FILE_BYTES: Final = 4096
"""A price file is a few hundred bytes; anything larger is never parsed (a boundary input)."""
STALE_DAYS: Final = 30
"""Provisional: a read date older than this warns in the run header; the lead may set it."""
RATE_NAMES: Final = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")
_KEYS: Final = frozenset(
    {"model", "source", "read_on", "usd_per_million_tokens", "max_cost_usd_per_task"}
)
_PER_MILLION: Final = 1_000_000
MAX_SOURCE_CHARS: Final = 200
"""PRC-09: a pricing page's URL; anything longer is not a plain page."""


@dataclass(frozen=True)
class PriceFile:
    price: TokenPrice
    max_cost_usd: float
    stale: bool
    """The read date is older than `STALE_DAYS` (the header warns; the run still starts)."""


def load_price(path: Path, *, model: str, today: date) -> PriceFile:
    """The price file at `path` for the run's `model`, checked whole; refuses on any fault."""
    doc = _read(path)
    if set(doc) - _KEYS:
        raise _refused("price_unknown_key")
    if doc.get("model") != model:
        raise _refused("price_model")
    rates = _rates(doc.get("usd_per_million_tokens"))
    cap = doc.get("max_cost_usd_per_task")
    if not _finite_positive(cap):
        raise _refused("price_cost_cap")
    source = doc.get("source")
    if not _plain_page(source):
        raise _refused("price_source")
    read_on = _day(doc.get("read_on"), today)
    price = TokenPrice(
        usd_per_input_token=rates[0],
        usd_per_output_token=rates[1],
        usd_per_cache_write_5m_token=rates[2],
        usd_per_cache_write_1h_token=rates[3],
        usd_per_cache_read_token=rates[4],
        source=source,
        read_on=read_on,
    )
    stale = (today - read_on).days > STALE_DAYS
    return PriceFile(price=price, max_cost_usd=float(cap), stale=stale)  # type: ignore[arg-type]  # checked by _finite_positive


def _read(path: Path) -> dict[str, object]:
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise _refused("price_missing") from exc
    except OSError as exc:
        raise _refused("price_unreadable") from exc
    if size > MAX_PRICE_FILE_BYTES:
        raise _refused("price_unreadable")
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_no_duplicates)
    except _DuplicateKeyError as exc:
        raise _refused("price_duplicate_key") from exc
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _refused("price_unreadable") from exc
    if not isinstance(doc, dict):
        raise _refused("price_unreadable")
    return doc


def _rates(table: object) -> tuple[float, float, float, float, float]:
    if not isinstance(table, dict) or set(table) != set(RATE_NAMES):
        raise _refused("price_rate")
    values = [table[name] for name in RATE_NAMES]
    if not all(_finite_positive(v) for v in values):
        raise _refused("price_rate")
    per_token = [float(v) / _PER_MILLION for v in values]
    if not all(rate > 0 for rate in per_token):  # PRC-02: a denormal rate underflows to 0.0
        raise _refused("price_rate")
    return per_token[0], per_token[1], per_token[2], per_token[3], per_token[4]


def _day(value: object, today: date) -> date:
    if not isinstance(value, str):
        raise _refused("price_read_on")
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise _refused("price_read_on") from exc
    if day > today or len(value) != len("YYYY-MM-DD"):
        raise _refused("price_read_on")
    return day


def _finite_positive(value: object) -> bool:
    """A number above zero and finite; a bool is never a number here."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        number = float(value)
    except OverflowError:  # PRC-01: an integer beyond any float
        return False
    return math.isfinite(number) and number > 0


def _plain_page(source: object) -> TypeGuard[str]:
    """PRC-09: an https page with a host and nothing else: no userinfo, query or fragment, at
    most `MAX_SOURCE_CHARS`, printable, no spaces (it is printed in the header and the trace)."""
    if not (
        isinstance(source, str)
        and len(source) <= MAX_SOURCE_CHARS
        and source.isprintable()
        and " " not in source
    ):
        return False
    try:
        parts = urlsplit(source)
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and "@" not in parts.netloc
        and not parts.query
        and not parts.fragment
        and "?" not in source
        and "#" not in source
    )


class _DuplicateKeyError(ValueError):
    pass


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """PRC-05: a key given twice is refused, never last-one-wins."""
    doc: dict[str, object] = {}
    for key, value in pairs:
        if key in doc:
            raise _DuplicateKeyError(key)
        doc[key] = value
    return doc


def _refused(token: str) -> SteerError:
    return SteerError(ErrorCode.GATE_BLOCKED, token)
