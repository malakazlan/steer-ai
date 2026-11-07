"""The runner wires the switch tap into the real session (issue #38): `_build_session` with
COM, UIA and the warm-up faked."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

import steerai.run as run_mod


def test_the_real_session_brings_a_switched_window_forward_through_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #38: the runner wires the tap and its guard (the executor's own readers) into the
    session; without them the loop never taps. COM, UIA and the warm-up are faked."""
    import steerai.action.execution as execution_mod  # noqa: PLC0415
    import steerai.action.executor as executor_mod  # noqa: PLC0415
    import steerai.action.launcher as launcher_mod  # noqa: PLC0415
    import steerai.action.live_reads as live_reads_mod  # noqa: PLC0415
    import steerai.action.physical as physical_mod  # noqa: PLC0415
    import steerai.orchestrator.loop as loop_mod  # noqa: PLC0415
    import steerai.probes.uia_snapshot as uia_mod  # noqa: PLC0415
    import steerai.world.model as model_mod  # noqa: PLC0415
    from steerai.probes import win_input  # noqa: PLC0415

    client: object = SimpleNamespace(dpi_per_monitor_v2=True)
    built: dict[str, Any] = {}
    monkeypatch.setattr(uia_mod, "UiaClient", lambda: client)
    monkeypatch.setattr(execution_mod.UiaBindings, "load", staticmethod(lambda: None))
    monkeypatch.setattr(model_mod, "WorldModel", lambda _client: SimpleNamespace())
    monkeypatch.setattr(run_mod, "warm_up", lambda *_a, **_k: None)
    for module, name in (
        (executor_mod, "Executor"),
        (physical_mod, "PhysicalExecutor"),
        (launcher_mod, "Launcher"),
        (live_reads_mod, "UiaLiveReads"),
    ):
        monkeypatch.setattr(module, name, lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr(loop_mod, "Session", lambda **kwargs: built.update(kwargs))
    monkeypatch.setattr(physical_mod, "tap_refusal", lambda inputs: inputs)
    bound = SimpleNamespace(
        hwnd=0x10,
        task_id="t",
        options=SimpleNamespace(warmup_s=0.0, verify_deadline_s=1.0, max_steps=5, goal=None),
        confirmer=None,
        ledger=None,
        trace=None,
        monitor=None,
    )
    run_mod._build_session(cast("run_mod.Bound", bound))
    assert built["bring_forward"] is win_input.acquire_foreground
    guard_inputs = built["tap_guard"]()
    assert isinstance(guard_inputs, physical_mod.LiveInput)
    assert guard_inputs._client is client, "the guard reads through the session's own client"
