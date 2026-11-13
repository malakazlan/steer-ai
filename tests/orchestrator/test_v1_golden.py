"""v1's behaviour, pinned (V2 step 1 packet, invariant 1; principal re-review item 4).

A fixed scenario set runs on the scripted world with its fake clock. The fixture holds what
each produced at efef4a2: every step result, the task result, every trace line and the fakes'
call logs. Observability work may add only the closed keys in `ADDED`, nothing else; a
difference anywhere else is a v1 behaviour change.

Regenerate only on purpose:
`STEER_GOLDEN_WRITE=1 uv run pytest tests/orchestrator/test_v1_golden.py`.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.orchestrator import test_no_effect_loop as no_effect
from tests.orchestrator import test_tree_expand_loop as tree
from tests.orchestrator import test_url_query_loop as url
from tests.orchestrator.test_loop import Harness, _send, _type

FIXTURE = Path(__file__).parent / "golden" / "v1_loop.json"
ADDED = frozenset({"url_proof", "url_reads", "aim"})
"""The keys V2 step 1 adds (packet §C); stripped before the comparison, nowhere else."""
_IDS = re.compile(r"\b(d-)?[0-9a-f]{12}\b")
"""Decision and snapshot ids are random per run (uuid4 hex[:12])."""


def _strip(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k not in ADDED}
    if isinstance(value, list | tuple):
        return [_strip(v) for v in value]
    if isinstance(value, str):
        return _IDS.sub("<id>", value)
    return value


def _record(h: Harness, results: list[Any], task: Any = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "results": [r.to_dict() for r in results],
        "trace": [json.loads(line) for line in h.trace.lines],
        "semantic_calls": [list(c) for c in h.semantic.calls],
        "physical_calls": [list(c) for c in h.physical.calls],
        "clock": h.clock["t"],
        **extra,
    }
    if task is not None:
        out["task"] = {
            "status": task.status,
            "code": None if task.code is None else task.code.value,
            "evidence": task.evidence,
            "ticked": sorted(task.ticked),
        }
    normalised: dict[str, Any] = json.loads(json.dumps(_strip(out), sort_keys=True, default=str))
    return normalised


def _type_send() -> dict[str, Any]:
    h = Harness()
    results = [h.session.run_step(_type()), h.session.run_step(_send())]
    return _record(h, results)


def _url(bar: url.Bar, **kw: Any) -> dict[str, Any]:
    h = url._harness(bar, **kw)
    result = h.session.run_step(url._submit())
    return _record(h, [result], bar_reads=len(bar.reads))


def _no_effect() -> dict[str, Any]:
    h, bar, _windows = no_effect._dead()
    task = h.session.run_task(no_effect._plan())
    return _record(h, task.results, task, bar_reads=len(bar.reads))


def _tree(*, opens: bool) -> dict[str, Any]:
    h, _boxes = tree._harness(opens=opens)
    results = [h.session.run_step(tree._expand()), h.session.run_step(tree._expand())]
    return _record(h, results)


SCENARIOS: dict[str, Callable[[], dict[str, Any]]] = {
    "type_then_send": _type_send,
    "url_counted": lambda: _url(url.Bar(url.HOME, url.RESULTS)),
    "url_no_words": lambda: _url(url.Bar(url.HOME, url.HOME)),
    "url_no_url": lambda: _url(url.Bar(None, None)),
    "url_no_load": lambda: _url(url.Bar(url.HOME, url.RESULTS), loaded="Bing"),
    "url_not_chrome": lambda: _url(url.Bar(url.HOME, url.RESULTS), image="notepad.exe"),
    "no_effect_task": _no_effect,
    "tree_click_twice": lambda: _tree(opens=True),
    "tree_page_ignores_click": lambda: _tree(opens=False),
}


def _current() -> dict[str, Any]:
    return {name: run() for name, run in SCENARIOS.items()}


def test_the_golden_fixture_is_written_only_on_purpose() -> None:
    if os.environ.get("STEER_GOLDEN_WRITE") != "1":
        pytest.skip("set STEER_GOLDEN_WRITE=1 to rewrite the fixture")
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_bytes((json.dumps(_current(), indent=1, sort_keys=True) + "\n").encode())


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_v1_behaviour_is_the_golden(name: str) -> None:
    golden = json.loads(FIXTURE.read_text("utf-8"))
    assert _strip(golden[name]) == SCENARIOS[name](), name


def test_the_scenarios_cover_the_url_proof_branches() -> None:
    """The fixture is only as good as its coverage: each scenario reached the leg it names."""
    golden = json.loads(FIXTURE.read_text("utf-8"))
    reasons = {name: golden[name]["results"][-1]["evidence"] for name in golden}
    assert reasons["url_counted"]["reason"] == "url_query"
    assert reasons["url_no_url"].get("detail") == "no_url"
    assert reasons["url_no_load"].get("detail") == "no_load"
    assert "detail" not in reasons["url_no_words"], "the words-miss branch returns no token"
    assert reasons["no_effect_task"]["detail"] == "no_effect"
