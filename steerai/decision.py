"""The System 1 decision interface (design 5.2, 5.8): model-agnostic by construction.

Jev implements it natively; `ClaudeChooser` implements it as a stand-in. Thresholds and calibration
are keyed by (question family, backend, model id) [v0.9]; an unknown key means bootstrap mode.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

NONE_OPTION = "none"


@dataclass(frozen=True, slots=True)
class ElementChoice:
    """Dense distribution over every option (each element id plus `none`), as Jev returns it."""

    element_id: int | None
    confidence: float
    margin: float
    probabilities: dict[str, float]
    model: str
    latency_ms: float


class ElementChooser(Protocol):
    def choose_element(self, *, intent: str, lines: Sequence[str]) -> ElementChoice: ...


def option_ids(lines: Sequence[str]) -> list[str]:
    """Element ids from numbered lines only (`<digits> ...`); trailers and headers are context."""
    ids: list[str] = []
    for line in lines:
        token = line.split(" ", 1)[0]
        if token.isdigit():
            ids.append(token)
    return ids


def top2_margin(probabilities: dict[str, float]) -> float:
    ordered = sorted(probabilities.values(), reverse=True)
    if not ordered:
        return 0.0
    return ordered[0] - (ordered[1] if len(ordered) > 1 else 0.0)


def peakedness(probabilities: dict[str, float]) -> float:
    """Confidence as Jev defines it over ALL options: 1.0 when all mass is on one, 0.0 when uniform.

    (k * max - 1) / (k - 1) for k options. Callers pass the dense distribution (every option
    present, 0.0 where unranked) so k is the number of options, not the number the model listed.
    """
    k = len(probabilities)
    if k == 0:
        return 0.0
    if k == 1:
        return 1.0
    return max(0.0, (k * max(probabilities.values()) - 1.0) / (k - 1.0))
