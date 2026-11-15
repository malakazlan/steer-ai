"""Adapter slice 4: the decide port ([V2.40] Q4, [V2.41] (1), packet section 2.9, #326).

The controller's `ask_model` runs each provider call on a daemon worker with no COM, and the
loop thread waits on it with a 50 ms kill-aware poll. A kill abandons the wait
(`PortInterrupted`). A result whose task generation is no longer current is discarded. The
abandoned call counts as a model call with its usage incomplete. The worker returns data only.
The runner treats `PortInterrupted` as no authority and re-reads `interrupted()`.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Mapping
from types import SimpleNamespace

import anthropic
import httpx2 as httpx
import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.loop2.controller import ControllerError, DecideRecord
from steerai.loop2.decide_port import (
    POLL_S,
    DecideCalls,
    Reply,
    TaskGenerations,
    anthropic_call,
    parse_usage,
)
from steerai.loop2.loop import PortInterrupted, TokenUsage
from steerai.loop2.runner import run_task
from tests.loop2.world_fakes import World, task

# ---- the usage object ([V2.45]) -----------------------------------------------------------------

PLAIN: Mapping[str, object] = {
    "input_tokens": 15,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
    "output_tokens": 4,
    "output_tokens_details": {"thinking_tokens": 0},
    "service_tier": "standard",
    "inference_geo": "global",
    "speed": "standard",
}
"""Recorded verbatim from the wire (claude-opus-5, SDK 2.4.0, 2026-11-15): a call with no cache.
Counts only: no key, no prompt text, no reply text."""
WRITE_5M: Mapping[str, object] = {
    "input_tokens": 13,
    "cache_creation_input_tokens": 8503,
    "cache_read_input_tokens": 0,
    "cache_creation": {"ephemeral_5m_input_tokens": 8503, "ephemeral_1h_input_tokens": 0},
    "output_tokens": 4,
    "output_tokens_details": {"thinking_tokens": 0},
    "service_tier": "standard",
    "inference_geo": "global",
    "speed": "standard",
}
"""Recorded verbatim: the same call that wrote a 5-minute cache entry."""
READ: Mapping[str, object] = {
    "input_tokens": 13,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 8503,
    "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
    "output_tokens": 4,
    "output_tokens_details": {"thinking_tokens": 0},
    "service_tier": "standard",
    "inference_geo": "global",
    "speed": "standard",
}
"""Recorded verbatim: the next call, reading that entry."""
RECORDED = (PLAIN, WRITE_5M, READ)


def _total(raw: Mapping[str, object]) -> int:
    """The API's own total (prompt-caching docs, read 2026-11-15): input_tokens +
    cache_creation_input_tokens + cache_read_input_tokens, plus the output."""
    names = (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
    )
    counts = [raw[name] for name in names]
    assert all(isinstance(c, int) for c in counts)
    return sum(c for c in counts if isinstance(c, int))


@pytest.mark.parametrize("raw", RECORDED)
def test_a_recorded_usage_sums_to_the_apis_total_with_nothing_counted_twice(
    raw: Mapping[str, object],
) -> None:
    """[V2.45]: the categories partition the API's total. The writes come from the breakdown and
    never also from `cache_creation_input_tokens`."""
    usage = parse_usage(raw)
    assert usage is not None
    categories = (
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_write_5m,
        usage.cache_write_1h,
        usage.cache_read,
    )
    assert sum(categories) == _total(raw)


def test_a_recorded_five_minute_write_is_read_by_lifetime() -> None:
    assert parse_usage(WRITE_5M) == TokenUsage(
        input_tokens=13, output_tokens=4, cache_write_5m=8503, cache_write_1h=0, cache_read=0
    )
    assert parse_usage(READ) == TokenUsage(input_tokens=13, output_tokens=4, cache_read=8503)


def test_a_one_hour_write_is_its_own_category() -> None:
    raw = dict(WRITE_5M)
    raw["cache_creation_input_tokens"] = 300
    raw["cache_creation"] = {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200}
    usage = parse_usage(raw)
    assert usage is not None
    assert (usage.cache_write_5m, usage.cache_write_1h) == (100, 200)


def test_a_write_with_no_lifetime_breakdown_is_no_usage() -> None:
    """A write that cannot be split by lifetime cannot be priced: incomplete, never guessed."""
    raw = {k: v for k, v in WRITE_5M.items() if k != "cache_creation"}
    assert parse_usage(raw) is None
    raw["cache_creation"] = None
    assert parse_usage(raw) is None


def test_no_write_needs_no_breakdown() -> None:
    raw = {k: v for k, v in READ.items() if k != "cache_creation"}
    assert parse_usage(raw) == TokenUsage(input_tokens=13, output_tokens=4, cache_read=8503)


@pytest.mark.parametrize(
    "field",
    ["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"],
)
@pytest.mark.parametrize("bad", ["missing", None, -1, True, 1.5, "3"])
def test_a_missing_or_invalid_count_is_no_usage(field: str, bad: object) -> None:
    raw = dict(PLAIN)
    if bad == "missing":
        del raw[field]
    else:
        raw[field] = bad
    assert parse_usage(raw) is None


@pytest.mark.parametrize(
    "breakdown",
    [
        {"ephemeral_5m_input_tokens": 8000, "ephemeral_1h_input_tokens": 0},  # sums short
        {"ephemeral_5m_input_tokens": 8503},  # a field missing
        {"ephemeral_5m_input_tokens": 8503, "ephemeral_1h_input_tokens": -1},
        {"ephemeral_5m_input_tokens": True, "ephemeral_1h_input_tokens": 8502},
        {"ephemeral_5m_input_tokens": 8503, "ephemeral_1h_input_tokens": 0, "ephemeral_24h": 7},
        "8503",
    ],
)
def test_a_breakdown_that_disagrees_with_the_total_is_no_usage(breakdown: object) -> None:
    raw = dict(WRITE_5M)
    raw["cache_creation"] = breakdown
    assert parse_usage(raw) is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("audio_input_tokens", 5),
        ("server_tool_use", {"web_search_requests": 1}),
        ("new_counter", 0.5),
        ("new_counters", [1]),
    ],
)
def test_an_unknown_counter_fails_closed(key: str, value: object) -> None:
    """[V2.45]: a counter the parser does not know is never dropped: the usage is incomplete."""
    raw = dict(PLAIN)
    raw[key] = value
    assert parse_usage(raw) is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("server_tool_use", None),
        ("server_tool_use", {"web_search_requests": 0, "web_fetch_requests": 0}),
        ("audio_input_tokens", 0),
        ("region_label", "global"),
    ],
)
def test_an_unknown_field_that_counts_nothing_is_ignored(key: str, value: object) -> None:
    raw = dict(PLAIN)
    raw[key] = value
    assert parse_usage(raw) == parse_usage(PLAIN)


def test_the_output_breakdown_is_inside_the_output_count() -> None:
    """The SDK documents `output_tokens` as the inclusive total and `output_tokens_details` as
    its decomposition: thinking tokens are never added again."""
    raw = dict(PLAIN)
    raw["output_tokens_details"] = {"thinking_tokens": 3}
    usage = parse_usage(raw)
    assert usage is not None
    assert usage.output_tokens == 4


# ---- the calls: a daemon worker, data only, a kill-aware wait -----------------------------------


def _usage(raw: Mapping[str, object]) -> TokenUsage:
    usage = parse_usage(raw)
    assert usage is not None
    return usage


def _reply(text: str = '{"stop":"cannot"}', raw: Mapping[str, object] = PLAIN) -> Reply:
    return Reply(text=text, stop_reason="end_turn", usage=_usage(raw))


def _serve(*replies: Reply) -> Callable[[list[str], list[str]], Reply]:
    """A provider call that returns `replies` in order, one per call."""
    queue = list(replies)

    def call(system: list[str], blocks: list[str]) -> Reply:
        del system, blocks
        return queue.pop(0)

    return call


class Never:
    """`interrupted()` that never reports, counting its polls."""

    def __init__(self) -> None:
        self.polls = 0

    def __call__(self) -> str | None:
        self.polls += 1
        return None


def _calls(
    call: Callable[[list[str], list[str]], Reply],
    interrupted: Callable[[], str | None] | None = None,
    generations: TaskGenerations | None = None,
) -> DecideCalls:
    return DecideCalls(
        call,
        interrupted=Never() if interrupted is None else interrupted,
        generations=TaskGenerations() if generations is None else generations,
    )


def test_the_poll_is_fifty_milliseconds() -> None:
    assert POLL_S == 0.05


def test_the_call_runs_on_a_daemon_worker_and_its_text_comes_back() -> None:
    seen: list[threading.Thread] = []

    def call(system: list[str], blocks: list[str]) -> Reply:
        seen.append(threading.current_thread())
        assert (system, blocks) == (["rules"], ["screen"])
        return _reply("answer")

    calls = _calls(call)
    text, usage = calls.turn(lambda ask: ask(["rules"], ["screen"]))
    assert text == "answer"
    assert usage == _usage(PLAIN)
    assert len(seen) == 1
    assert seen[0] is not threading.current_thread()
    assert seen[0].daemon


def test_the_turn_sums_usage_over_its_calls_repairs_included() -> None:
    calls = _calls(_serve(_reply(raw=WRITE_5M), _reply(raw=READ)))
    _, usage = calls.turn(lambda ask: (ask([], []), ask([], [])))
    assert usage == TokenUsage(
        input_tokens=26, output_tokens=8, cache_write_5m=8503, cache_write_1h=0, cache_read=8503
    )


def test_one_call_with_no_usage_makes_the_turn_usage_none() -> None:
    calls = _calls(_serve(_reply(), Reply(text="x", stop_reason="end_turn", usage=None)))
    _, usage = calls.turn(lambda ask: (ask([], []), ask([], [])))
    assert usage is None


def test_a_turn_starts_with_no_usage_from_the_last_one() -> None:
    calls = _calls(_serve(_reply(), _reply()))
    calls.turn(lambda ask: ask([], []))
    _, usage = calls.turn(lambda ask: ask([], []))
    assert usage == _usage(PLAIN)


def test_a_refusal_is_a_model_error() -> None:
    calls = _calls(_serve(Reply(text="", stop_reason="refusal", usage=_usage(PLAIN))))
    with pytest.raises(SteerError) as err:
        calls.turn(lambda ask: ask([], []))
    assert err.value.code is ErrorCode.MODEL_ERROR
    assert err.value.message == "refusal"


def test_the_workers_error_is_raised_on_the_loop_thread() -> None:
    failure = SteerError(ErrorCode.MODEL_TIMEOUT, "timeout")

    def call(system: list[str], blocks: list[str]) -> Reply:
        raise failure

    with pytest.raises(SteerError) as err:
        _calls(call).turn(lambda ask: ask([], []))
    assert err.value is failure


class Held:
    """A provider call that blocks until released, then returns `reply`."""

    def __init__(self, reply: Reply) -> None:
        self.reply = reply
        self.started = threading.Event()
        self.release = threading.Event()
        self.returned = threading.Event()

    def __call__(self, system: list[str], blocks: list[str]) -> Reply:
        del system, blocks
        self.started.set()
        assert self.release.wait(10)
        self.returned.set()
        return self.reply


class KillAfter:
    """`interrupted()` that reports `killed` from its `n`-th poll on, once the call started."""

    def __init__(self, held: Held, n: int = 2) -> None:
        self.held = held
        self.n = n
        self.polls = 0

    def __call__(self) -> str | None:
        if not self.held.started.is_set():
            return None
        self.polls += 1
        return "killed" if self.polls >= self.n else None


def test_a_kill_during_the_call_abandons_the_wait() -> None:
    held = Held(_reply("late"))
    generations = TaskGenerations()
    before = generations.current()
    calls = _calls(held, KillAfter(held), generations)
    with pytest.raises(PortInterrupted) as err:
        calls.turn(lambda ask: ask([], []))
    assert err.value.token == "killed"  # noqa: S105 - an interrupt token, not a password
    assert not held.returned.is_set()  # the wait ended while the call was still running
    assert generations.current() != before  # the abandoned call's result is stale from here
    held.release.set()
    assert held.returned.wait(10)


def test_a_result_returning_after_the_kill_never_reaches_a_turn() -> None:
    held = Held(_reply("late", raw=WRITE_5M))
    fresh = Reply(text="fresh", stop_reason="end_turn", usage=_usage(READ))
    providers: list[Callable[[list[str], list[str]], Reply]] = [held, _serve(fresh)]
    kill = KillAfter(held)
    killing = [True]

    def provider(system: list[str], blocks: list[str]) -> Reply:
        return providers.pop(0)(system, blocks)

    calls = _calls(provider, lambda: kill() if killing[0] else None)
    with pytest.raises(PortInterrupted):
        calls.turn(lambda ask: ask([], []))
    held.release.set()
    assert held.returned.wait(10)  # the late result has landed in the abandoned call's box
    killing[0] = False  # the next task: no kill
    text, usage = calls.turn(lambda ask: ask([], []))
    assert (text, usage) == ("fresh", _usage(READ))


def test_a_result_returning_after_a_new_task_started_is_discarded() -> None:
    generations = TaskGenerations()

    def call(system: list[str], blocks: list[str]) -> Reply:
        generations.bump()  # a new task starts while this call is on the wire
        return _reply("old task")

    calls = _calls(call, generations=generations)
    with pytest.raises(PortInterrupted) as err:
        calls.turn(lambda ask: ask([], []))
    assert err.value.token == "generation"  # noqa: S105 - an interrupt token, not a password


def test_the_wait_polls_interrupted_while_the_call_runs() -> None:
    held = Held(_reply())
    never = Never()
    calls = _calls(held, never)
    done = threading.Event()

    def turn() -> None:
        calls.turn(lambda ask: ask([], []))
        done.set()

    loop = threading.Thread(target=turn, daemon=True)
    loop.start()
    assert held.started.wait(10)
    while never.polls < 3:  # each poll waits POLL_S on the worker's done event
        assert loop.is_alive()
        threading.Event().wait(POLL_S)
    held.release.set()
    assert done.wait(10)


def test_the_worker_is_given_data_and_the_call_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker holds no port, no runner and no `interrupted` or generation: it gets the call,
    the prompt blocks and a result box, and returns data only."""
    started: list[tuple[object, ...]] = []
    real = threading.Thread

    def spy(
        *, target: Callable[..., None], args: tuple[object, ...], name: str, daemon: bool
    ) -> threading.Thread:
        started.append(args)
        assert daemon is True
        return real(target=target, args=args, name=name, daemon=daemon)

    monkeypatch.setattr("steerai.loop2.decide_port.threading.Thread", spy)
    never = Never()
    generations = TaskGenerations()
    call = Held(_reply())
    call.release.set()
    calls = _calls(call, never, generations)
    calls.turn(lambda ask: ask(["s"], ["b"]))
    (args,) = started
    assert args[0] is call
    assert args[1:3] == (["s"], ["b"])
    for arg in args:
        assert arg is not calls
        assert arg is not never
        assert arg is not generations


def test_a_reply_is_frozen() -> None:
    reply = _reply()
    with pytest.raises(AttributeError):
        reply.text = "changed"  # type: ignore[misc]  # asserting immutability


# ---- the provider call (the SDK wrapper the worker runs) ----------------------------------------


class FakeMessages:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.kwargs: dict[str, object] = {}

    def create(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.response


class FakeUsage:
    def __init__(self, raw: Mapping[str, object]) -> None:
        self.raw = raw

    def model_dump(self, *, mode: str) -> Mapping[str, object]:
        assert mode == "json"
        return self.raw


def _response(stop: str = "end_turn") -> object:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="hidden"),
            SimpleNamespace(type="text", text='{"stop":'),
            SimpleNamespace(type="text", text='"cannot"}'),
        ],
        stop_reason=stop,
        usage=FakeUsage(WRITE_5M),
    )


def test_the_provider_call_sends_v1s_model_and_effort_and_reads_text_and_usage() -> None:
    messages = FakeMessages(_response())
    call = anthropic_call(SimpleNamespace(messages=messages), max_tokens=4096)
    reply = call(["rules", "schema"], ["stable", "turn"])
    assert reply == Reply(text='{"stop":"cannot"}', stop_reason="end_turn", usage=_usage(WRITE_5M))
    assert messages.kwargs["model"] == "claude-opus-5"
    assert messages.kwargs["output_config"] == {"effort": "medium"}
    assert messages.kwargs["max_tokens"] == 4096
    assert messages.kwargs["system"] == [
        {"type": "text", "text": "rules"},
        {"type": "text", "text": "schema"},
    ]
    assert messages.kwargs["messages"] == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "stable"}, {"type": "text", "text": "turn"}],
        }
    ]


REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (anthropic.APITimeoutError(request=REQUEST), ErrorCode.MODEL_TIMEOUT),
        (anthropic.APIConnectionError(request=REQUEST), ErrorCode.MODEL_ERROR),
        (
            anthropic.APIStatusError(
                "overloaded", response=httpx.Response(529, request=REQUEST), body=None
            ),
            ErrorCode.MODEL_ERROR,
        ),
    ],
)
def test_sdk_failures_map_to_the_model_codes(error: Exception, code: ErrorCode) -> None:
    call = anthropic_call(SimpleNamespace(messages=FakeMessages(error=error)), max_tokens=64)
    with pytest.raises(SteerError) as err:
        call([], [])
    assert err.value.code is code
    assert err.value.__cause__ is error


def test_an_unknown_failure_is_not_mapped() -> None:
    boom = KeyError("x")
    call = anthropic_call(SimpleNamespace(messages=FakeMessages(error=boom)), max_tokens=64)
    with pytest.raises(KeyError):
        call([], [])


# ---- the runner: PortInterrupted has no authority; an abandoned call is counted -----------------


def test_a_port_interrupt_with_the_switch_latched_stops_with_its_token() -> None:
    world = World([], decide_raises=PortInterrupted("killed"))
    world.interruption = "takeover"
    world.interrupt_in_decide = 1  # the latched switch reads `takeover`
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "takeover")
    assert world.dispatched == []


def test_a_port_interrupt_with_the_switch_unlatched_is_a_port_failure() -> None:
    world = World([], decide_raises=PortInterrupted("killed"))
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "port_interrupted_unlatched")
    assert world.dispatched == []


def test_an_abandoned_call_counts_as_a_model_call_with_usage_incomplete() -> None:
    world = World([], decide_raises=PortInterrupted("killed"))
    world.interrupt_in_decide = 1
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == 1
    assert outcome.usage_incomplete
    assert math.isnan(outcome.tokens)


def test_a_failed_call_counts_as_a_model_call_with_usage_incomplete() -> None:
    world = World([], decide_raises=SteerError(ErrorCode.MODEL_TIMEOUT, "timeout"))
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "port_model_timeout")
    assert outcome.model_calls == 1
    assert outcome.usage_incomplete


def test_a_controller_failure_after_calling_counts_the_call() -> None:
    error = ControllerError("not_json", record=DecideRecord("controller-4", 2, True, True))
    world = World([], decide_raises=error)
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "port_not_json")
    assert outcome.model_calls == 1
    assert outcome.usage_incomplete


def test_a_controller_failure_before_any_call_counts_nothing() -> None:
    world = World([], decide_raises=ControllerError("fence_bad"))
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "port_fence_bad")
    assert outcome.model_calls == 0
    assert not outcome.usage_incomplete
