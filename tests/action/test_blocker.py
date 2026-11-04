"""The kill check at the injection choke point (TK-06/09/10, principal item 2): every
mutating call, semantic or physical, asks the blocker first; typing asks before every burst."""

from __future__ import annotations

from typing import Any

import pytest

from steerai.action.dispositions import Delivery
from steerai.action.execution import Call, RefusalError, UiaBindings
from steerai.action.executor import Action, Executor
from steerai.action.physical import PhysicalExecutor
from steerai.errors import ErrorCode
from tests.action.fakes import TYPE_IDS, FakeHandle, FakeWindows
from tests.action.test_executor import _snapshot as _semantic_snapshot
from tests.action.test_executor import fast_clock as semantic_clock  # noqa: F401 - fixture
from tests.action.test_physical import FakeInput, _edit, _snapshot, _type
from tests.action.test_physical import fast_clock as physical_clock  # noqa: F401 - fixture


def test_the_call_asks_the_blocker_before_mutating() -> None:
    sent: list[str] = []
    call = Call("click", "semantic", blocker=lambda: None)
    call.send("x", lambda: sent.append("x"))
    assert sent == ["x"]
    stopped = Call("click", "semantic", blocker=lambda: "killed")
    with pytest.raises(RefusalError) as info:
        stopped.send("y", lambda: sent.append("y"))
    assert info.value.code is ErrorCode.GATE_BLOCKED
    assert info.value.detail == "killed"
    assert sent == ["x"]
    assert stopped.sent is None, "nothing was issued"


def test_a_semantic_click_is_refused_with_the_token_and_nothing_invoked(
    semantic_clock: None,  # noqa: F811 - fixture
) -> None:
    handle = FakeHandle()
    executor = Executor(
        uia=UiaBindings(control_types={v: k for k, v in TYPE_IDS.items()}, interfaces={}),
        windows=FakeWindows(),
        clock=lambda: 0.0,
        blocker=lambda: "killed",
    )
    result = executor.execute(Action(verb="click", element_id=1), _semantic_snapshot(handle))
    assert handle.invoked == 0
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "killed"


def test_typing_stops_between_bursts_with_the_kill_token(physical_clock: Any) -> None:  # noqa: F811
    handle = _edit()
    inputs = FakeInput(sink=handle)
    flag = {"kill": False}

    def blocker() -> str | None:
        return "killed" if flag["kill"] else None

    original = inputs.type_text

    def type_then_kill(*args: Any, **kwargs: Any) -> Any:
        flag["kill"] = True  # the hot key lands during the first burst
        return original(*args, **kwargs)

    inputs.type_text = type_then_kill  # type: ignore[method-assign]  # a stand-in for the hot key
    executor = PhysicalExecutor(
        uia=UiaBindings(control_types={v: k for k, v in TYPE_IDS.items()}, interfaces={}),
        windows=FakeWindows(),
        clock=lambda: 0.0,
        inputs=inputs,
        blocker=blocker,
    )
    result = executor.execute(_type("hello world, café"), _snapshot(handle, role="Edit"))
    assert inputs.bursts == ["text:8"], "one burst went out, nothing after the flag"
    assert result.delivery is Delivery.DELIVERY_UNCERTAIN
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["reason"] == "killed"
    assert result.evidence["chunks"] == 1


def test_a_kill_before_the_first_burst_is_a_refusal(physical_clock: Any) -> None:  # noqa: F811
    handle = _edit()
    inputs = FakeInput(sink=handle)
    executor = PhysicalExecutor(
        uia=UiaBindings(control_types={v: k for k, v in TYPE_IDS.items()}, interfaces={}),
        windows=FakeWindows(),
        clock=lambda: 0.0,
        inputs=inputs,
        blocker=lambda: "killed",
    )
    result = executor.execute(_type("hello"), _snapshot(handle, role="Edit"))
    assert inputs.bursts == []
    assert result.delivery is Delivery.NOT_DELIVERED
    assert result.code is ErrorCode.GATE_BLOCKED
    assert result.evidence["detail"] == "killed"
