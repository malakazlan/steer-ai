"""Invariant 9, #468: budgets count real calls. A model call is counted only when its request went
out: a count request is no call, and a wait given up before the send (`PortInterrupted.sent` False)
is no call. After the send, a reply or a provider error is one."""

from __future__ import annotations

import dataclasses
import math
from typing import Any

import pytest

from steerai.errors import ErrorCode, SteerError
from steerai.loop2 import controller
from steerai.loop2.decide_port import DecideCalls, Reply, TaskGenerations
from steerai.loop2.loop import CountFailed, PortInterrupted
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
