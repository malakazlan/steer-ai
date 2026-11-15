"""The decide port's provider calls (adapter slice 4; [V2.40] Q4, Q5; [V2.41] (1); packet 2.9).

`DecideCalls.turn` hands the controller an `ask_model` that runs each provider call on its own
daemon worker thread. The worker is plain Python with no COM: it gets the call, the prompt blocks
and a result box, and returns data only (`Reply`). The loop thread waits on the box and polls
`interrupted()` every `POLL_S`.
- **A kill abandons the wait.** It raises `PortInterrupted` with the token and bumps the task
  generation; the HTTP call is not cancelled and runs on to its reply or its timeout. A result
  whose generation is no longer current (a kill, or a new task started) is discarded.
- **Usage** is summed over the turn's calls, repairs included. One call with no usage makes the
  turn's usage None, which trips the token budget ([V2.37] P6).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, TypeVar

from steerai.errors import ErrorCode, SteerError
from steerai.loop2.controller import AskModel
from steerai.loop2.loop import PortInterrupted, TokenUsage
from steerai.planner.claude_planner import DEFAULT_MAX_TOKENS, DEFAULT_MODEL, EFFORT

POLL_S: Final = 0.05
"""[V2.40] Q4: the loop thread polls `interrupted()` this often while a call runs (packet 5)."""

_COUNTS: Final = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
_TTL: Final = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
_NOT_COUNTERS: Final = frozenset({"cache_creation", "output_tokens_details"})
"""Usage fields that are breakdowns of a count already read: `cache_creation` splits the writes,
and `output_tokens_details` decomposes `output_tokens`, the inclusive total (SDK 2.4.0's
`Usage` docstring). Neither adds tokens."""

T = TypeVar("T")


@dataclass(frozen=True)
class Reply:
    """What a provider call returns to the loop thread: plain values only."""

    text: str
    stop_reason: str | None
    usage: TokenUsage | None


Call = Callable[[list[str], list[str]], Reply]
"""One provider call: the system blocks and the user blocks in, a `Reply` out. It runs on the
decide worker, so it must hold no port, no executor and no COM object."""


def parse_usage(raw: Mapping[str, object]) -> TokenUsage | None:
    """[V2.45]: the API's usage object (as JSON) as a `TokenUsage`, the writes split by lifetime.
    None (incomplete, never a guess) when:
    - any of the four counts is missing or not a whole number of tokens, zero or more;
    - a write cannot be split: no `cache_creation` breakdown, or one that does not split
      `cache_creation_input_tokens` exactly into the two lifetimes;
    - any other field holds a count that is not zero (an unknown counter is never dropped).
    Measured on claude-opus-5, 2026-11-15: all four counts and the breakdown are sent, 0 unused."""
    counts = [_count(raw.get(name)) for name in _COUNTS]
    fresh, output, written, read = counts
    if fresh is None or output is None or written is None or read is None:
        return None
    lifetimes = _lifetimes(raw.get("cache_creation"), written)
    if lifetimes is None:
        return None
    known = {*_COUNTS, *_NOT_COUNTERS}
    if any(not _counts_nothing(value) for key, value in raw.items() if key not in known):
        return None
    five, hour = lifetimes
    return TokenUsage(
        input_tokens=fresh,
        output_tokens=output,
        cache_write_5m=five,
        cache_write_1h=hour,
        cache_read=read,
    )


def _lifetimes(breakdown: object, written: int) -> tuple[int, int] | None:
    """The writes as (5 minutes, 1 hour); None when they cannot be split exactly."""
    if breakdown is None:
        return (0, 0) if written == 0 else None
    if not isinstance(breakdown, Mapping) or set(breakdown) != set(_TTL):
        return None
    five, hour = (_count(breakdown.get(name)) for name in _TTL)
    if five is None or hour is None or five + hour != written:
        return None
    return five, hour


def _counts_nothing(value: object) -> bool:
    """An unknown field that counts nothing: absent, a label, a zero, or only such values."""
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, Mapping):
        return all(_counts_nothing(v) for v in value.values())
    if isinstance(value, bool):
        return False
    return isinstance(value, int | float) and value == 0


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class TaskGenerations:
    """The task generation: bumped when a task starts and when one is killed. A provider result
    is used only while the generation it was asked under is current."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def current(self) -> int:
        with self._lock:
            return self._value

    def bump(self) -> int:
        with self._lock:
            self._value += 1
            return self._value


LateError = Callable[[str], None]
"""Where a late failure is reported: a closed token, never the error's message. It may be
called from the decide worker's thread, so it must be thread-safe (the adapter's clear trace)."""


LATE_CLASSES: Final = frozenset(
    {
        "ConnectionError",
        "KeyError",
        "OSError",
        "RuntimeError",
        "TimeoutError",
        "TypeError",
        "ValueError",
    }
)
"""#346 (1): the exception classes a late token may name; any other class (a library's, or one
whose name a page could shape) is `late_error`."""


def late_token(error: BaseException) -> str:
    """A closed token for a failure nobody will read: `late_<code>` for a SteerError,
    `late_<class>` for a class on the allowlist, else `late_error`. Never the message, which
    may quote page or prompt text."""
    if isinstance(error, SteerError):
        return f"late_{error.code.value}"
    name = type(error).__name__
    return f"late_{name.lower()}" if name in LATE_CLASSES else "late_error"


class _Box:
    """Where the worker leaves its reply or its failure. Written once, under the lock, with
    `done` set inside it, so an abandoned call's failure is reported exactly once: by `finish`
    when it lands after the abandon, or by `abandon` when it landed before and went unread."""

    def __init__(self, late_error: LateError) -> None:
        self.done = threading.Event()
        self.reply: Reply | None = None
        self.error: BaseException | None = None
        self._late_error = late_error
        self._lock = threading.Lock()
        self._abandoned = False

    def finish(self, reply: Reply | None, error: BaseException | None) -> None:
        with self._lock:
            self.reply, self.error = reply, error
            self.done.set()
            late = error if self._abandoned else None
        if late is not None:
            self._late_error(late_token(late))

    def abandon(self) -> None:
        with self._lock:
            self._abandoned = True
            unread = self.error if self.done.is_set() else None
        if unread is not None:
            self._late_error(late_token(unread))

    def result(self) -> Reply:
        if self.error is not None:
            raise self.error
        if self.reply is None:
            raise AssertionError("the worker finished with no reply and no error")
        return self.reply


def _work(call: Call, system: list[str], blocks: list[str], box: _Box) -> None:
    """The decide worker: one provider call; its reply or its failure, whatever its class, left
    in the box. `finally` finishes the box even if the catch were ever narrowed. A `SystemExit`
    here is kept like any failure and raised on the loop thread; `run_on_loop_thread` re-raises
    it in the caller, so it ends the process (#346 (2))."""
    reply: Reply | None = None
    error: BaseException | None = None
    try:
        reply = call(system, blocks)
    except BaseException as exc:  # noqa: BLE001 - raised on the loop thread as itself
        error = exc
    finally:
        box.finish(reply, error)


class DecideCalls:
    """The controller's `ask_model` for one task's turns: each call on a daemon worker, the wait
    kill-aware, the usage summed per turn."""

    def __init__(
        self,
        call: Call,
        *,
        interrupted: Callable[[], str | None],
        generations: TaskGenerations,
        late_error: LateError,
        poll_s: float = POLL_S,
    ) -> None:
        self._call = call
        self._interrupted = interrupted
        self._generations = generations
        self._late_error = late_error
        self._poll_s = poll_s
        self._usages: list[TokenUsage | None] = []

    def turn(self, run: Callable[[AskModel], T]) -> tuple[T, TokenUsage | None]:
        """Run one controller turn with this `ask_model`; its result and the summed usage of
        its calls (None when a call reported none, or no call was made)."""
        self._usages = []
        result = run(self._ask)
        return result, _summed(self._usages)

    def _ask(self, system: list[str], blocks: list[str]) -> str:
        self._check(None)  # nothing is sent after the latch: not even a repair ask
        generation = self._generations.current()
        box = _Box(self._late_error)
        worker = threading.Thread(
            target=_work,
            args=(self._call, list(system), list(blocks), box),
            name="steer-decide",
            daemon=True,  # an abandoned call never holds the process open at exit
        )
        worker.start()
        while not box.done.wait(self._poll_s):
            self._check(box)
        self._check(box)  # a reply inside one poll window: the switch was never read
        if self._generations.current() != generation:
            box.abandon()
            raise PortInterrupted("generation")  # killed, or a new task, while it ran
        reply = box.result()
        self._usages.append(reply.usage)
        if reply.stop_reason == "refusal":
            raise SteerError(ErrorCode.MODEL_ERROR, "refusal")
        return reply.text

    def _check(self, box: _Box | None) -> None:
        """Read the switch: on a token, the generation moves on (any result in flight is stale),
        the call is abandoned, and the wait ends with `PortInterrupted`."""
        token = self._interrupted()
        if token is None:
            return
        self._generations.bump()
        if box is not None:
            box.abandon()
        raise PortInterrupted(token)


def _summed(usages: list[TokenUsage | None]) -> TokenUsage | None:
    known = [u for u in usages if u is not None]
    if not usages or len(known) != len(usages):
        return None
    return TokenUsage(
        input_tokens=sum(u.input_tokens for u in known),
        output_tokens=sum(u.output_tokens for u in known),
        cache_write_5m=sum(u.cache_write_5m for u in known),
        cache_write_1h=sum(u.cache_write_1h for u in known),
        cache_read=sum(u.cache_read for u in known),
    )


def anthropic_call(
    client: Any,  # Any: the SDK's `Anthropic` client, or a double with `.messages.create`
    *,
    model: str = DEFAULT_MODEL,
    effort: str = EFFORT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Call:
    """[V2.40] Q5: the provider call with v1's planner model and effort. The client carries v1's
    timeout and retry (`DEFAULT_TIMEOUT_S`, `DEFAULT_MAX_RETRIES`). SDK failures map to the model
    codes; any other error is not mapped and propagates."""

    def call(system: list[str], blocks: list[str]) -> Reply:
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": s} for s in system],
                messages=[
                    {"role": "user", "content": [{"type": "text", "text": b} for b in blocks]}
                ],
                output_config={"effort": effort},
            )
        except Exception as exc:
            mapped = _sdk_failure(exc)
            if mapped is None:
                raise
            raise mapped from exc
        text = "".join(block.text for block in response.content if block.type == "text")
        stop = response.stop_reason
        return Reply(
            text=text,
            stop_reason=None if stop is None else str(stop),
            usage=parse_usage(response.usage.model_dump(mode="json")),
        )

    return call


def _sdk_failure(exc: Exception) -> SteerError | None:
    """The SDK's failures as model codes (as v1's planner maps them); the timeout first, since
    `APITimeoutError` is an `APIConnectionError`."""
    import anthropic  # noqa: PLC0415 - the SDK is loaded only when a call fails

    if isinstance(exc, anthropic.APITimeoutError):
        return SteerError(ErrorCode.MODEL_TIMEOUT, "timeout")
    if isinstance(exc, anthropic.APIStatusError):
        return SteerError(ErrorCode.MODEL_ERROR, f"http {exc.status_code}")
    if isinstance(exc, anthropic.APIConnectionError | anthropic.CredentialsError):
        return SteerError(ErrorCode.MODEL_ERROR, "sdk")
    return None
