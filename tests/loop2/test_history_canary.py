"""#347 review (the tester's canary, kept as a regression test): no page text reaches the history
or anything the controller sees outside the screen fence.

Distinct lowercase canaries, in the alphabet the history allows, for every text field: the name,
automation id, localized control type and ARIA role of two elements, the field's name, id and
value, the window title, the process, the URL and the Document URL. They go through
`run_task` -> `make_decide` -> `build_prompt`; none may appear in the history, the system
blocks, the stable block, the log block or the turn block outside the screen fence, and no
history line may be dropped.
"""

from __future__ import annotations

import dataclasses
import json

from steerai.loop2.decide_port import DecideCalls, Reply, TaskGenerations
from steerai.loop2.history import LogEntry
from steerai.loop2.loop import TokenUsage
from steerai.loop2.prompt_inputs import make_decide
from steerai.loop2.runner import run_task
from steerai.world.effects import ValueFact
from tests.loop2.world_fakes import (
    FIELD,
    GO,
    PAGE,
    World,
    act,
    decision,
    element,
    observed,
    report,
    task,
)

CANARY = {
    "name": "zqnamecanary",
    "aid": "zqaidcanary",
    "ctype": "zqlocalctype",
    "aria": "zqariacanary",
    "title": "zqtitlecanary",
    "value": "zqvaluecanary",
    "proc": "zqproccanary",
    "url": "zqurlcanary",
    "name2": "zqnametwo",
    "aid2": "zqaidtwo",
    "url2": "zqdocurlcanary",
}


class _Provider:
    def __init__(self) -> None:
        self.prompts: list[tuple[list[str], list[str]]] = []

    def __call__(self, system: list[str], blocks: list[str]) -> Reply:
        self.prompts.append((system, blocks))
        usage = TokenUsage(input_tokens=1, output_tokens=1)
        return Reply(text=json.dumps({"stop": "cannot"}), stop_reason="end_turn", usage=usage)


def _world() -> World:
    go = dataclasses.replace(GO, name=CANARY["name"], automation_id=CANARY["aid"])
    odd = dataclasses.replace(
        element(50, CANARY["ctype"], CANARY["name2"], (9, 50), 1), automation_id=CANARY["aid2"]
    )
    field = dataclasses.replace(
        FIELD, name=CANARY["name2"] + "f", automation_id=CANARY["aid2"] + "f"
    )
    swap = {GO.id: go, FIELD.id: field}
    screen = observed([*(swap.get(e.id, e) for e in PAGE), odd])
    screen = dataclasses.replace(
        screen,
        snapshot=dataclasses.replace(
            screen.snapshot, title=CANARY["title"], process=CANARY["proc"]
        ),
        url=f"https://{CANARY['url']}.test/",
        document_url=f"https://{CANARY['url2']}.test/",
        aria_roles={
            **(screen.aria_roles or {}),
            go.runtime_id: CANARY["aria"],
            odd.runtime_id: CANARY["aria"],
        },
    )
    world = World(
        [
            decision(act("click", target=GO.id), first=True),
            decision(act("click", target=odd.id)),
            decision(act("type", target=FIELD.id, param="text")),
        ],
        effects=[report("content_changed"), report("content_changed"), report("own_value")],
        screen=screen,
    )
    world.values[FIELD.runtime_id] = ValueFact(filled=True, digest=CANARY["value"])
    return world


def test_no_canary_reaches_the_history_or_the_prompt_outside_the_screen_fence() -> None:
    world = _world()
    outcome = run_task(task(), world.ports())
    view = world.views[-1]
    acts = [e for e in view.log if isinstance(e, LogEntry)]
    assert len(acts) == 3, view.log
    assert acts[1].role == "unknown"  # a localized control type is never shown
    assert outcome.history_dropped == 0
    provider = _Provider()
    calls = DecideCalls(
        provider,
        interrupted=lambda: None,
        generations=TaskGenerations(),
        late_error=lambda _t: None,
    )
    make_decide(calls, goal=task().goal, task_fence="taskfence01")(view)
    system, blocks = provider.prompts[0]
    history = "\n".join(view.history) + "\n" + blocks[1]
    stable = "\n".join(system) + blocks[0]
    turn = blocks[2]
    start, end = turn.index("<<<screen "), turn.rindex(">>>")
    outside = turn[:start] + turn[end:]
    for key, canary in CANARY.items():
        assert canary not in history, key
        assert canary not in stable, key
        assert canary not in outside, key
    assert CANARY["name"] in turn  # shown, inside the screen fence
    assert CANARY["title"] in turn
