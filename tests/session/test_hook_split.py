"""[V2.60] hook helper, PR 1 (packet v2-hook-helper rev 2b §10.1): the pure split.

The helper process loads only the classifier, the probe ledger and the layout. None of them may
reach input-sending code (`steerai.action`, `steerai.probes`) or the in-process monitor, at any
depth (S6). The split changes no behaviour: `MonitorCore` is their composition and its own tests
pass unchanged."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from steerai.probes.win_input import STEER_EXTRA_INFO
from steerai.session.hook_classifier import (
    LLKHF_UP,
    OTHER_STEER_INPUT,
    TAKEOVER,
    V1_TAG,
    WM_LBUTTONDOWN,
    WM_MOUSEMOVE,
    Classifier,
)
from steerai.session.probe_ledger import ProbeLedger

ROOT = Path(__file__).resolve().parents[2]
HELPER_MODULES = (
    "steerai.session.hook_classifier",
    "steerai.session.probe_ledger",
    "steerai.session.hook_layout",
)
FORBIDDEN = ("steerai.action", "steerai.probes", "steerai.session.monitor")


def _path(module: str) -> Path:
    base = ROOT.joinpath(*module.split("."))
    package = base / "__init__.py"
    return package if package.is_file() else base.with_suffix(".py")


def _steerai_imports(module: str) -> set[str]:
    """Every `steerai.*` module `module` imports, at module level or inside a function."""
    tree = ast.parse(_path(module).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return {name for name in found if name.split(".")[0] == "steerai" and _path(name).is_file()}


def _closure(module: str) -> set[str]:
    seen: set[str] = set()
    todo = [module]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        parts = name.split(".")
        parents = [".".join(parts[:i]) for i in range(2, len(parts))]  # packages run __init__
        todo.extend(p for p in parents if _path(p).is_file())
        todo.extend(_steerai_imports(name))
    return seen


@pytest.mark.parametrize("module", HELPER_MODULES)
def test_the_helpers_modules_never_reach_input_sending_code(module: str) -> None:
    """S6: the helper never sends input, so nothing it loads can."""
    reached = _closure(module)
    assert not [m for m in reached if m.startswith(FORBIDDEN)], sorted(reached)


def test_the_closure_walker_does_see_a_forbidden_import() -> None:
    """The walker itself: the in-process monitor reaches input-sending code."""
    assert any(m.startswith("steerai.probes") for m in _closure("steerai.session.monitor"))


def test_v1s_tag_is_the_one_the_input_side_sends() -> None:
    assert V1_TAG == STEER_EXTRA_INFO


# ---- the classifier, alone -------------------------------------------------------------------

RUN_TAG = 0x1234_5678
PROBE = 0x0E


def _classifier() -> Classifier:
    return Classifier(started_at=10.0, extra_info=RUN_TAG, probe_vk=PROBE)


def test_only_the_probe_keys_own_key_up_is_a_keyboard_delivery() -> None:
    c = _classifier()
    c.on_keyboard(flags=0, extra=RUN_TAG, now=11.0, vk=PROBE)  # key-down: not the probe
    c.on_keyboard(flags=LLKHF_UP, extra=RUN_TAG, now=11.1, vk=0x41)  # own, another key
    assert c.delivered("keyboard") == 0
    c.on_keyboard(flags=LLKHF_UP, extra=RUN_TAG, now=11.2, vk=PROBE)
    assert c.delivered("keyboard") == 1
    assert c.last_own("keyboard") == 11.2


def test_every_own_mouse_event_is_a_mouse_delivery() -> None:
    c = _classifier()
    c.on_mouse(message=WM_MOUSEMOVE, flags=0, extra=RUN_TAG, pt=(5, 5), now=12.0)
    assert c.delivered("mouse") == 1
    assert c.last_own("mouse") == 12.0


def test_a_persons_input_and_another_steers_are_told_apart() -> None:
    c = _classifier()
    assert c.last_user() is None
    c.on_mouse(message=WM_LBUTTONDOWN, flags=0, extra=0, pt=(1, 1), now=13.0)
    assert (c.last_user(), c.last_kind()) == (13.0, TAKEOVER)
    c.on_keyboard(flags=0, extra=V1_TAG, now=14.0, vk=0x41)
    assert (c.last_user(), c.last_kind()) == (14.0, OTHER_STEER_INPUT)


def test_input_while_an_ask_is_open_is_the_answer() -> None:
    c = _classifier()
    c.mark_ask_open()
    c.on_keyboard(flags=0, extra=0, now=15.0, vk=0x41)
    assert c.last_user() is None
    assert c.counts()["answer"] == 1


# ---- the probe ledger, alone -----------------------------------------------------------------


def test_the_ledger_proves_only_when_both_hooks_delivered_every_probe() -> None:
    c = _classifier()
    ledger = ProbeLedger(per_hook=True, deadline_s=1.05)
    ledger.probe_sent(now=20.0, counts=c)
    c.on_mouse(message=WM_MOUSEMOVE, flags=0, extra=RUN_TAG, pt=(5, 5), now=20.01)
    assert not ledger.probe_seen(counts=c)  # the keyboard has not seen it
    c.on_keyboard(flags=LLKHF_UP, extra=RUN_TAG, now=20.02, vk=PROBE)
    assert ledger.probe_seen(counts=c)


def test_the_ledger_names_the_hook_that_missed_its_deadline() -> None:
    c = _classifier()
    ledger = ProbeLedger(per_hook=True, deadline_s=1.05)
    ledger.probe_sent(now=20.0, counts=c)
    c.on_mouse(message=WM_MOUSEMOVE, flags=0, extra=RUN_TAG, pt=(5, 5), now=20.01)
    assert ledger.lost_hooks(now=21.0, counts=c) == ()
    assert ledger.lost_hooks(now=21.1, counts=c) == ("keyboard",)
