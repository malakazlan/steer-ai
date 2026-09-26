"""Jev (TypeSafe System One) client for Steer's typed decisions (design 5.2, 5.8).

One question family per method. Everything the model sees is text; the caller prunes it.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx2
from typesafe_sdk import Choice, TypeSafeClient

NONE_OPTION = "none"
_ELEMENT_KEY = "target"


@dataclass(frozen=True, slots=True)
class ElementChoice:
    element_id: int | None
    confidence: float
    margin: float
    probabilities: dict[str, float]
    model: str
    latency_ms: float


def build_element_question(*, intent: str, lines: Sequence[str]) -> tuple[dict[str, str], Choice]:
    """State + Choice over element ids (design 5.8: one Choice, ids as options, `none` included)."""
    criteria: dict[str, str | None] = {line.split(" ", 1)[0]: line for line in lines}
    criteria[NONE_OPTION] = "No listed element is the right target for this intent."
    state = {"intent": intent, "screen": "\n".join(lines)}
    question = Choice(
        instructions=(
            "Which element in `screen` should be acted on next to carry out `intent`? "
            "Pick the single best element id. Pick `none` if no listed element fits."
        ),
        criteria=criteria,
    )
    return state, question


class JevClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "jev-latest",
        transport: httpx2.BaseTransport | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._client = TypeSafeClient(
            api_key=api_key, model=model, transport=transport, timeout=timeout_s
        )

    def choose_element(self, *, intent: str, lines: Sequence[str]) -> ElementChoice:
        state, question = build_element_question(intent=intent, lines=lines)
        started = time.perf_counter_ns()
        response = self._client.system_one(state=state, questions={_ELEMENT_KEY: question})
        latency_ms = (time.perf_counter_ns() - started) / 1_000_000
        answer: Any = response.answers[_ELEMENT_KEY]  # SDK answer union; narrowed by key
        probabilities = {str(k): float(v) for k, v in dict(answer.probabilities).items()}
        chosen = str(answer.choice)
        return ElementChoice(
            element_id=None if chosen == NONE_OPTION else int(chosen),
            confidence=float(answer.confidence),
            margin=_top2_margin(probabilities),
            probabilities=probabilities,
            model=str(response.model),
            latency_ms=latency_ms,
        )


def _top2_margin(probabilities: dict[str, float]) -> float:
    ordered = sorted(probabilities.values(), reverse=True)
    if not ordered:
        return 0.0
    return ordered[0] - (ordered[1] if len(ordered) > 1 else 0.0)
