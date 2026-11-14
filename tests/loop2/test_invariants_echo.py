"""[V2.15] Q8: on the echo page's variants e1 to e3 (packet v2-step4-claims §3.7) nothing above R1
is dispatched. The scripted world plays a searchbox field whose Enter is a query and a page that
only echoes the query back. Red on the 3c loop stub."""

from __future__ import annotations

import pytest

from steerai.loop2.loop import run_task
from steerai.world.effects import EffectReport
from tests.loop2.world_fakes import FIELD, World, act, decision, observed, pending, report, task

TYPE = act("type", target=FIELD.id, param="text")
ENTER = act("press", key="Enter")

ECHO: dict[str, list[EffectReport]] = {
    "e1_title": [report("own_value"), report(tokens=["title_only"])],
    "e2_title_and_url": [report("own_value"), report("url_changed_in_place")],
    "e3_heading": [report("own_value"), report("content_changed")],
}


@pending("[V2.15] Q8: nothing above R1 is dispatched on e1 to e3")
@pytest.mark.parametrize("variant", sorted(ECHO))
def test_an_echo_page_never_gets_an_action_above_r1(variant: str) -> None:
    world = World(
        [decision(TYPE, ENTER, first=True)],
        effects=list(ECHO[variant]),
        screen=observed(focus=FIELD.runtime_id),
    )
    run_task(task(), world.ports())
    assert [d.action.verb for d in world.dispatched] == ["type", "press"]
    assert all(d.risk in ("R0", "R1") for d in world.dispatched)
    assert world.write_aheads == []
