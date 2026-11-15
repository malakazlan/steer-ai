"""V2 loop (behind `--loop v2`). Nothing on the v1 path imports this package: `steerai.run`
imports it only inside the `--loop v2` branch."""

from __future__ import annotations


def ready(step: int) -> bool:
    """Whether the v2 loop is built through `step` (packet §7): False while any interface of
    that step or an earlier one is still a stub. Reads the stub registry only; it calls nothing
    live."""
    from steerai.loop2 import (  # noqa: F401, PLC0415 - importing registers each module's stubs
        act_guards,
        act_port,
        actuate,
        batch,
        claims,
        cli,
        consent,
        controller,
        decide_port,
        decision,
        form,
        guards,
        history,
        identity_cost,
        ime_gate,
        listing,
        live_world,
        loop,
        owned,
        price,
        prompt_inputs,
        query,
        record,
        runner,
        stage1,
        stage1_expected,
        stage2_expected,
        task,
        text,
        unstick_probe,
        verify,
        world_ports,
    )
    from steerai.loop2._stub import PENDING_STEP  # noqa: PLC0415 - after the modules registered

    return not any(stub_step <= step for stub_step in PENDING_STEP.values())
