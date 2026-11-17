"""Invariant 9, #468: budgets count real calls. A model call is counted only when its request went
out: a count request is no call, and a wait given up before the send (`PortInterrupted.sent` False)
is no call. After the send, a reply or a provider error is one."""

from __future__ import annotations

import dataclasses
import math
import threading
from typing import Any

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.loop2 import controller
from steerai.loop2.decide_port import DecideCalls, Reply, TaskGenerations
from steerai.loop2.loop import CountFailed, PortInterrupted, TokenUsage, calls_made
from steerai.loop2.prompt_inputs import make_audit, make_intake
from steerai.loop2.runner import run_task
from tests.loop2.test_final_audit import _value_only
from tests.loop2.test_intake_check import VALUE, _first, _world_with
from tests.loop2.world_fakes import World, task

DECIDE_ONLY = 1  # the intake tests' first decide, before the check


def _calls(counter: Any) -> DecideCalls:  # Any: a counter double
    def refuse(_s: list[str], _b: list[str]) -> Reply:
        raise AssertionError("a request went out")

    return DecideCalls(
        refuse,
        interrupted=lambda: None,
        generations=TaskGenerations(),
        late_error=lambda _t: None,
        counter=counter,
    )


def _broken(_s: list[str], _b: list[str]) -> int:
    raise KeyError("x")


@pytest.fixture
def near_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every prompt is over the ceiling and inside the exact-count band."""
    monkeypatch.setattr(controller, "PROMPT_TOKEN_CEILING", 10)
    monkeypatch.setattr(controller, "EXACT_BAND", 10_000.0)


# ---- the count path ------------------------------------------------------------------------


def test_a_counters_unmapped_failure_is_a_count_failure_chained_to_its_cause() -> None:
    with pytest.raises(CountFailed) as raised:
        _calls(_broken).count(["s"], ["u"])
    assert isinstance(raised.value.__cause__, KeyError)


@pytest.mark.usefixtures("near_ceiling")
def test_a_count_failure_is_no_model_call_in_the_audit() -> None:
    world = _value_only(None)
    audit = make_audit(_calls(_broken), goal="g", task_fence="taskfence01")
    outcome = run_task(task(), dataclasses.replace(world.ports(), audit=audit))
    assert (outcome.status, outcome.reason) == ("blocked", "audit_failed")
    assert outcome.audit == ("failed", "audit_no_call")
    assert outcome.model_calls == world.events.count("decide")
    assert (outcome.audit_calls, outcome.audit_failed) == (0, 0)
    assert not outcome.usage_incomplete


@pytest.mark.usefixtures("near_ceiling")
def test_a_count_failure_is_no_model_call_in_the_intake() -> None:
    intake = make_intake(_calls(_broken), goal=task().goal, task_fence="taskfence01")
    world = _world_with([_first([VALUE])])
    outcome = run_task(task(), dataclasses.replace(world.ports(), intake=intake))
    assert outcome.reason == "port_model_error"
    assert outcome.model_calls == DECIDE_ONLY
    assert (outcome.intake_checks, outcome.intake_failed) == (0, 0)
    assert outcome.intake_no_call == 1
    assert not outcome.usage_incomplete


def test_a_count_failure_in_decide_is_no_model_call() -> None:
    world = World([], decide_raises=CountFailed("x"))
    outcome = run_task(task(), world.ports())
    assert outcome.reason == "port_model_error"
    assert outcome.model_calls == 0
    assert not outcome.usage_incomplete
    assert not math.isnan(outcome.tokens)


# ---- decide: before and after the send -----------------------------------------------------


def test_decide_given_up_before_the_send_counts_no_call() -> None:
    world = World([], decide_raises=PortInterrupted("killed", sent=False))
    world.interrupt_in_decide = 1
    world.interruption = "killed"
    outcome = run_task(task(), world.ports())
    assert outcome.reason == "killed"
    assert outcome.model_calls == 0
    assert not outcome.usage_incomplete
    assert not math.isnan(outcome.tokens)


def test_decide_given_up_after_the_send_counts_one_call() -> None:
    world = World([], decide_raises=PortInterrupted("killed", sent=True))
    world.interrupt_in_decide = 1
    world.interruption = "killed"
    outcome = run_task(task(), world.ports())
    assert (outcome.reason, outcome.model_calls) == ("killed", 1)
    assert outcome.usage_incomplete


def test_decide_provider_error_after_the_send_counts_one_call() -> None:
    world = World([], decide_raises=SteerError(ErrorCode.MODEL_TIMEOUT, "timeout"))
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == 1


# ---- the intake check ----------------------------------------------------------------------


def test_the_intake_given_up_before_the_send_counts_no_call() -> None:
    world = _world_with([_first([VALUE])], reply=PortInterrupted("killed", sent=False))
    world.interrupt_after_intake = True
    world.interruption = "killed"
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == DECIDE_ONLY
    assert (outcome.intake_checks, outcome.intake_failed) == (0, 0)


def test_the_intake_given_up_after_the_send_counts_one_call() -> None:
    world = _world_with([_first([VALUE])], reply=PortInterrupted("killed", sent=True))
    world.interrupt_after_intake = True
    world.interruption = "killed"
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == DECIDE_ONLY + 1
    assert (outcome.intake_checks, outcome.intake_failed) == (1, 1)


def test_the_intake_count_failure_error_is_no_call() -> None:
    world = _world_with([_first([VALUE])], reply=CountFailed("x"))
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == DECIDE_ONLY
    assert (outcome.intake_checks, outcome.intake_failed) == (0, 0)


# ---- the final audit -----------------------------------------------------------------------


def _audit_world(reply: BaseException) -> World:
    world = _value_only(reply)
    world.interrupt_after_audit = True
    world.interruption = "killed"
    return world


def test_the_audit_given_up_before_the_send_counts_no_call() -> None:
    world = _audit_world(PortInterrupted("killed", sent=False))
    outcome = run_task(task(), world.ports())
    assert outcome.reason == "killed"
    assert outcome.model_calls == world.events.count("decide")
    assert (outcome.audit_calls, outcome.audit_failed) == (0, 0)
    assert not outcome.usage_incomplete


def test_the_audit_given_up_after_the_send_counts_one_call() -> None:
    world = _audit_world(PortInterrupted("killed", sent=True))
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == world.events.count("decide") + 1
    assert (outcome.audit_calls, outcome.audit_failed) == (1, 1)
    assert outcome.usage_incomplete


def test_the_audit_count_failure_error_is_no_call() -> None:
    world = _value_only(CountFailed("x"))
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("blocked", "audit_failed")
    assert outcome.model_calls == world.events.count("decide")
    assert (outcome.audit_calls, outcome.audit_failed) == (0, 0)


def test_the_audit_provider_error_after_the_send_counts_one_call() -> None:
    world = _value_only(SteerError(ErrorCode.MODEL_TIMEOUT, "timeout"))
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == world.events.count("decide") + 1
    assert (outcome.audit_calls, outcome.audit_failed) == (1, 1)


# ---- the requests a turn sent: a repair is a second call (#489 principal) -------------------


def _turn_calls(interrupted: Any, provider: Any) -> DecideCalls:  # Any: test doubles
    return DecideCalls(
        provider,
        interrupted=interrupted,
        generations=TaskGenerations(),
        late_error=lambda _t: None,
    )


def _reply() -> Reply:
    return Reply("{}", "end_turn", TokenUsage(1, 1))


def _first_then_repair(ask: Any) -> None:  # Any: the turn's ask
    ask([], [])
    ask([], [])


def test_a_switch_latched_between_the_call_and_its_repair_reports_one_request() -> None:
    latch: list[str] = []

    def run(ask: Any) -> None:  # Any: the turn's ask
        ask([], [])
        latch.append("killed")  # the reply is in; the repair is about to be asked
        ask([], [])

    calls = _turn_calls(lambda: latch[0] if latch else None, lambda _s, _b: _reply())
    with pytest.raises(PortInterrupted) as raised:
        calls.turn(run)
    assert raised.value.sent is False
    assert calls_made(raised.value) == 1


def test_a_switch_latched_during_the_repair_reports_two_requests() -> None:
    sent: list[int] = []
    release = threading.Event()

    def provider(_s: list[str], _b: list[str]) -> Reply:
        sent.append(1)
        if len(sent) == 2:
            release.wait(5)
        return _reply()

    calls = _turn_calls(lambda: "killed" if len(sent) == 2 else None, provider)
    try:
        with pytest.raises(PortInterrupted) as raised:
            calls.turn(_first_then_repair)
    finally:
        release.set()
    assert calls_made(raised.value) == 2


def test_a_provider_error_on_the_repair_reports_two_requests() -> None:
    sent: list[int] = []

    def provider(_s: list[str], _b: list[str]) -> Reply:
        sent.append(1)
        if len(sent) == 2:
            raise SteerError(ErrorCode.MODEL_TIMEOUT, "timeout")
        return _reply()

    calls = _turn_calls(lambda: None, provider)
    with pytest.raises(SteerError) as raised:
        calls.turn(_first_then_repair)
    assert calls_made(raised.value) == 2


def test_the_requests_count_is_per_turn() -> None:
    calls = _turn_calls(lambda: None, lambda _s, _b: _reply())
    calls.turn(_first_then_repair)

    def interrupted_first() -> None:
        raise PortInterrupted("killed", sent=False)

    with pytest.raises(PortInterrupted) as raised:
        calls.turn(lambda _ask: interrupted_first())
    assert raised.value.requests_sent == 0


@pytest.mark.parametrize("sent", [0, 1, 2])
def test_the_runner_counts_the_requests_the_turn_sent(sent: int) -> None:
    error = PortInterrupted("killed", sent=False)
    error.requests_sent = sent
    world = World([], decide_raises=error)
    world.interrupt_in_decide = 1
    world.interruption = "killed"
    outcome = run_task(task(), world.ports())
    assert outcome.model_calls == sent
    assert outcome.usage_incomplete == (sent > 0)


def test_a_provider_error_after_two_requests_counts_two() -> None:
    error = SteerError(ErrorCode.MODEL_TIMEOUT, "timeout")
    error.requests_sent = 2
    outcome = run_task(task(), World([], decide_raises=error).ports())
    assert outcome.model_calls == 2


def test_an_intake_count_failure_is_a_no_call_cause() -> None:
    world = _world_with([_first([VALUE])], reply=CountFailed("x"))
    outcome = run_task(task(), world.ports())
    assert outcome.intake_no_call == 1


def test_an_audit_count_failure_is_a_no_call_cause() -> None:
    outcome = run_task(task(), _value_only(CountFailed("x")).ports())
    assert outcome.audit_no_call == 1
