"""The System 1 decision interface (design 5.2, 5.8): model-agnostic by construction.

Jev implements it natively; `ClaudeChooser` implements it as a stand-in. Thresholds, calibration
and the bootstrap-mode rules apply to whichever implementation is behind the interface.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from steerai.jev import ElementChoice


class ElementChooser(Protocol):
    def choose_element(self, *, intent: str, lines: Sequence[str]) -> ElementChoice: ...


def top2_margin(probabilities: dict[str, float]) -> float:
    ordered = sorted(probabilities.values(), reverse=True)
    if not ordered:
        return 0.0
    return ordered[0] - (ordered[1] if len(ordered) > 1 else 0.0)


def peakedness(probabilities: dict[str, float]) -> float:
    """Confidence as Jev defines it: 1.0 when all mass is on one option, 0.0 when uniform.

    (k * max - 1) / (k - 1) for k options; a single option is fully confident.
    """
    k = len(probabilities)
    if k == 0:
        return 0.0
    if k == 1:
        return 1.0
    return max(0.0, (k * max(probabilities.values()) - 1.0) / (k - 1.0))
