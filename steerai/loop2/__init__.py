"""V2 loop (behind `--loop v2`). Nothing on the v1 path imports this package: `steerai.run`
imports it only inside the `--loop v2` branch."""

from __future__ import annotations


def ready() -> bool:
    """Whether the v2 loop is built: False while any step 3c or step 4 interface is still a
    stub. Reads the stub registry only; it calls nothing live."""
    from steerai.loop2 import (  # noqa: F401, PLC0415 - importing registers each module's stubs
        actuate,
        batch,
        claims,
        consent,
        guards,
        loop,
        query,
        record,
        verify,
    )
    from steerai.loop2._stub import PENDING  # noqa: PLC0415 - read after the modules registered

    return not PENDING
