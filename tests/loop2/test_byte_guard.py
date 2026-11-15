"""[V2.61]: the prompt-size guard on UTF-8 bytes. An upper bound from bytes at the worst
measured tokens per byte plus a margin; near the ceiling an exact count; a failed or late exact
count falls back to cutting by the bound, never to sending; every reply's true input tokens are
compared with the estimate, and a low estimate is counted, flagged and tightens the ratio for the
rest of the task; names are capped in bytes."""

from __future__ import annotations

import json
import math
import threading
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2 as httpx
import pytest

from steerai.loop2 import controller
from steerai.loop2.controller import (
    EXACT_BAND,
    MAX_NAME_BYTES,
    TOKENS_PER_BYTE,
    ControllerError,
    ListingRow,
    decide,
    prompt_tokens,
    v2_listing,
)
from steerai.loop2.decide_port import (
    COUNT_TIMEOUT_S,
    DecideCalls,
    Reply,
    TaskGenerations,
    anthropic_count,
)
from steerai.loop2.loop import PortInterrupted, TokenUsage
from steerai.loop2.prompt_inputs import make_decide
from steerai.world.elements import MAX_NAME_CHARS, Element, format_lines, safe_name
from tests.loop2.test_listing_wiring import GOAL, _view
from tests.loop2.world_fakes import snapshot

WORST_MEASURED = 0.7388
"""count_tokens on claude-opus-5, 2026-11-15, the v2 prompt at the structural maximum (300
rows, 120-character names): symbols 0.7388 tokens per byte, the worst of ASCII 0.3942, CJK
0.5597, Hangul 0.6217, emoji 0.6612 and mixed 0.6701."""


def test_the_bound_is_bytes_at_the_worst_measured_ratio_plus_a_margin() -> None:
    assert TOKENS_PER_BYTE >= WORST_MEASURED * 1.1 - 1e-9
    emoji = "\U0001f600" * 100  # 400 bytes
    assert prompt_tokens([emoji], []) == math.ceil(400 * TOKENS_PER_BYTE)
    assert prompt_tokens(["abcd"], [], per_byte=1.0) == 4


def _rows(n: int, name: str = "x" * 100) -> list[ListingRow]:
    return [ListingRow(1, f'{i} Button "{name}"', handle=i, row_class="rest")
            for i in range(1, n + 1)]  # fmt: skip


def _ask(_system: list[str], _blocks: list[str]) -> str:
    return json.dumps({"stop": "cannot"})


def _decide_with(
    rows: list[ListingRow],
    ceiling: int,
    exact: Any,
    monkeypatch: pytest.MonkeyPatch,  # Any: a fake
) -> tuple[list[tuple[frozenset[int], int]], list[int]]:
    monkeypatch.setattr(controller, "PROMPT_TOKEN_CEILING", ceiling)
    fits: list[tuple[frozenset[int], int]] = []
    sent: list[int] = []
    decide(
        _ask, task_goal=GOAL, subgoal_lines=[], action_log=[], subgoal_status=[],
        window_title="Shop", listing_rows=rows, listed_ids=frozenset(r.handle for r in rows
        if r.handle is not None), first_turn=True, task_fence="taskfence01",
        on_fit=lambda shown, cut: fits.append((shown, cut)), count_exact=exact,
        on_sent=sent.append,
    )  # fmt: skip
    return fits, sent


def _bound_of(rows: list[ListingRow]) -> int:
    system, user = controller.build_prompt(
        GOAL, [], [], [], "Shop", rows, task_fence="taskfence01", screen_fence="f" * 32
    )
    return prompt_tokens(system, user)


def test_near_the_ceiling_an_exact_count_that_fits_sends_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _rows(40)
    ceiling = _bound_of(rows) - 50  # the bound is just over; the truth is far under
    calls: list[int] = []

    def exact(_system: list[str], _user: list[str]) -> int:
        calls.append(1)
        return ceiling - 1

    fits, sent = _decide_with(rows, ceiling, exact, monkeypatch)
    assert (fits[0][1], calls) == (0, [1])
    assert sent[0] > (ceiling / TOKENS_PER_BYTE)  # sent over the bound, on the exact count


@pytest.mark.parametrize("answer", [None, 10**9], ids=["failed_or_late", "over"])
def test_an_exact_count_that_fails_or_is_over_cuts_by_the_bound(
    answer: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows(40)
    ceiling = _bound_of(rows) - 50
    calls: list[int] = []

    def exact(_system: list[str], _user: list[str]) -> int | None:
        calls.append(1)
        return answer

    fits, _ = _decide_with(rows, ceiling, exact, monkeypatch)
    assert fits[0][1] > 0  # rows were cut: never sent on a hope
    assert calls == [1]  # one exact count a turn


def test_far_over_the_ceiling_no_exact_count_is_spent(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _rows(300)
    ceiling = int(_bound_of(rows) / (EXACT_BAND + 0.5))  # over the prompt with no rows
    calls: list[int] = []
    _decide_with(rows, ceiling, lambda _s, _u: calls.append(1), monkeypatch)
    assert calls == []


def test_without_an_exact_counter_the_bound_alone_decides(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _rows(40)
    ceiling = _bound_of(rows) - 50
    fits, sent = _decide_with(rows, ceiling, None, monkeypatch)
    assert fits[0][1] > 0
    assert math.ceil(sent[0] * TOKENS_PER_BYTE) <= ceiling


def test_a_prompt_that_cannot_fit_is_still_never_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ControllerError) as refused:
        _decide_with(_rows(5), 10, lambda _s, _u: None, monkeypatch)
    assert refused.value.code == "prompt_too_large"


# ---- names capped in bytes ---------------------------------------------------------------------


def test_v2_names_are_capped_in_bytes_and_v1s_lines_are_unchanged() -> None:
    emoji = "\U0001f600" * MAX_NAME_CHARS  # 480 bytes, within v1's character cap
    element = Element(id=1, depth=0, role="Button", name=emoji, box=(0, 0, 9, 9),
                      runtime_id=(1, 1))  # fmt: skip
    row = v2_listing(snapshot([element]), handles={1: 1})[0]
    name = row.text.split('"')[1]
    assert len(name.encode("utf-8")) <= MAX_NAME_BYTES
    assert format_lines([element])[0].count("\U0001f600") == MAX_NAME_CHARS  # v1 as it was
    ascii_name = "a" * 300
    plain = Element(id=2, depth=0, role="Button", name=ascii_name, box=(0, 0, 9, 9),
                    runtime_id=(1, 2))  # fmt: skip
    v2_plain = v2_listing(snapshot([plain]), handles={2: 2})[0].text.split('"')[1]
    assert v2_plain == safe_name(ascii_name)  # ASCII within the byte cap: v1's own cut


# ---- self-calibration ---------------------------------------------------------------------------


class _Provider:
    """Each reply reports `true_in` input tokens."""

    def __init__(self, true_in: int) -> None:
        self.true_in = true_in

    def __call__(self, _system: list[str], _blocks: list[str]) -> Reply:
        return Reply(json.dumps({"stop": "cannot"}), "end_turn", TokenUsage(self.true_in, 5))


def _calls(provider: _Provider) -> DecideCalls:
    return DecideCalls(provider, interrupted=lambda: None, generations=TaskGenerations(),
                       late_error=lambda _t: None)  # fmt: skip


def test_a_true_count_over_the_estimate_is_flagged_and_tightens_the_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    used: list[float] = []
    real = controller.decide

    def spy(*args: Any, **kwargs: Any) -> Any:  # Any: decide's own arguments
        used.append(kwargs["tokens_per_byte"])
        return real(*args, **kwargs)

    monkeypatch.setattr("steerai.loop2.prompt_inputs.decide", spy)
    provider = _Provider(20_000)  # above this prompt's estimate, under the ceiling
    port = make_decide(_calls(provider), goal=GOAL, task_fence="taskfence01")
    first = port(_view())
    assert first.estimate_low is True
    second = port(_view())
    assert used[0] == TOKENS_PER_BYTE
    assert used[1] > TOKENS_PER_BYTE  # tightened for the rest of the task
    assert second.estimate_low is False  # the tightened ratio now covers it


def test_a_true_count_under_the_estimate_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    port = make_decide(_calls(_Provider(10)), goal=GOAL, task_fence="taskfence01")
    assert port(_view()).estimate_low is False


def test_an_exact_count_spent_is_reported_on_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(controller, "PROMPT_TOKEN_CEILING", 1)
    monkeypatch.setattr(controller, "EXACT_BAND", 10.0**9)
    calls = DecideCalls(_Provider(10), interrupted=lambda: None, generations=TaskGenerations(),
                        late_error=lambda _t: None, counter=lambda _s, _b: 1)  # fmt: skip
    decided = make_decide(calls, goal=GOAL, task_fence="taskfence01")(_view())
    assert decided.exact_counted is True


def test_a_failed_turn_still_tightens_and_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected turn's calls reported their usage: the bound is held against it too."""
    monkeypatch.setattr(controller, "PROMPT_TOKEN_CEILING", 1)
    monkeypatch.setattr(controller, "EXACT_BAND", 10.0**9)

    def bad(_system: list[str], _blocks: list[str]) -> Reply:
        return Reply("not json", "end_turn", TokenUsage(20_000, 5))

    calls = DecideCalls(bad, interrupted=lambda: None, generations=TaskGenerations(),
                        late_error=lambda _t: None, counter=lambda _s, _b: 1)  # fmt: skip
    with pytest.raises(ControllerError) as failed:
        make_decide(calls, goal=GOAL, task_fence="taskfence01")(_view())
    assert (failed.value.exact_counted, failed.value.estimate_low) == (True, True)


# ---- the exact count: kill-aware, timed out, never a hope ---------------------------------------


def test_the_count_timeout_is_derived_from_the_measured_max() -> None:
    assert pytest.approx(0.889 * 1.25) == COUNT_TIMEOUT_S


def _counting(counter: Any, **kwargs: Any) -> DecideCalls:  # Any: a fake counter; its knobs
    knobs: dict[str, Any] = {"interrupted": lambda: None, **kwargs}  # Any: as above
    return DecideCalls(_Provider(1), generations=TaskGenerations(), late_error=lambda _t: None,
                       counter=counter, **knobs)  # fmt: skip


def test_a_count_returns_the_counters_answer() -> None:
    assert _counting(lambda _s, _b: 77).count(["s"], ["u"]) == 77


def test_no_counter_is_no_count() -> None:
    assert _counting(None).count(["s"], ["u"]) is None


def test_a_late_count_is_none_and_the_wait_ends_at_the_timeout() -> None:
    release = threading.Event()

    def slow(_s: list[str], _b: list[str]) -> int:
        release.wait(5)
        return 1

    try:
        assert _counting(slow, count_timeout_s=0.05, poll_s=0.01).count(["s"], ["u"]) is None
    finally:
        release.set()


def test_a_kill_during_a_count_abandons_it() -> None:
    release = threading.Event()
    killed: list[str | None] = [None]

    def slow(_s: list[str], _b: list[str]) -> int:
        killed[0] = "user_takeover"
        release.wait(5)
        return 1

    port = _counting(slow, interrupted=lambda: killed[0], poll_s=0.01)
    try:
        with pytest.raises(PortInterrupted):
            port.count(["s"], ["u"])
    finally:
        release.set()


def test_a_counters_unmapped_failure_is_raised_as_itself() -> None:
    def broken(_s: list[str], _b: list[str]) -> int:
        raise KeyError("x")

    with pytest.raises(KeyError):
        _counting(broken).count(["s"], ["u"])


# ---- the SDK counter ---------------------------------------------------------------------------

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages/count_tokens")


class _Messages:
    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self.result, self.error = result, error
        self.kwargs: dict[str, Any] = {}  # Any: the SDK's keyword arguments

    def count_tokens(self, **kwargs: Any) -> object:  # Any: as above
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.result


def test_the_sdk_counter_counts_the_blocks_the_call_sends() -> None:
    messages = _Messages(SimpleNamespace(input_tokens=321))
    count = anthropic_count(SimpleNamespace(messages=messages))
    assert count(["rules"], ["turn"]) == 321
    assert messages.kwargs["model"] == "claude-opus-5"
    assert messages.kwargs["system"] == [{"type": "text", "text": "rules"}]
    assert messages.kwargs["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "turn"}]}
    ]


@pytest.mark.parametrize(
    "error",
    [
        anthropic.APITimeoutError(request=REQUEST),
        anthropic.APIConnectionError(request=REQUEST),
        anthropic.APIStatusError("overloaded", response=httpx.Response(529, request=REQUEST),
                                 body=None),
    ],
    ids=["timeout", "connection", "status"],
)  # fmt: skip
def test_an_sdk_failure_is_no_count(error: Exception) -> None:
    assert anthropic_count(SimpleNamespace(messages=_Messages(error=error)))([], []) is None


@pytest.mark.parametrize("value", [True, -1, 1.5, "9", None])
def test_a_count_that_is_not_a_non_negative_int_is_no_count(value: object) -> None:
    messages = _Messages(SimpleNamespace(input_tokens=value))
    assert anthropic_count(SimpleNamespace(messages=messages))([], []) is None


def test_an_unknown_sdk_counter_failure_propagates() -> None:
    with pytest.raises(KeyError):
        anthropic_count(SimpleNamespace(messages=_Messages(error=KeyError("x"))))([], [])
