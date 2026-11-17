"""Packet §7 (rev 4.0): `ready(step)`. Every stub carries its step; `ready(3)` is True only when no
stub of step 3 or below is left. Each step 4 stub is unreachable from the step 3 path (one test per
stub), a stub reached anyway ends the task with an internal error, and every pending xfail mark
carries its step with none left for step 3."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from steerai.loop2 import ready, runner
from steerai.loop2._stub import PENDING, PENDING_STEP, stub
from steerai.loop2.runner import run_task
from tests.loop2.world_fakes import GO, World, act, decision, task

ROOT = Path(__file__).resolve().parents[2]
LOOP2 = ROOT / "steerai" / "loop2"
TESTS = Path(__file__).resolve().parent
STEP_3_PATH = sorted(
    p
    for p in LOOP2.glob("*.py")
    if p.stem not in {"claims", "consent", "record", "_stub", "__init__"}
)
"""The modules a step 3 task runs through: everything in the package but the step 4 modules
(`claims`, `consent`, `record`), the registry and the package's `ready`."""


def _registered() -> dict[str, int]:
    ready(3)  # importing the package's modules registers their stubs
    return dict(PENDING_STEP)


def test_every_stub_carries_a_step() -> None:
    steps = _registered()
    assert set(steps) == PENDING
    assert all(step in {3, 4} for step in steps.values())


def test_ready_3_needs_no_step_3_stub_and_step_4_stays_not_ready() -> None:
    steps = _registered()
    assert [name for name, step in steps.items() if step <= 3] == []
    assert ready(3) is True
    assert ready(4) is False


def test_a_step_3_stub_makes_ready_3_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("steerai.loop2._stub.PENDING", set(PENDING))
    monkeypatch.setattr("steerai.loop2._stub.PENDING_STEP", dict(PENDING_STEP))

    @stub(3)
    def not_built() -> None:
        raise NotImplementedError("step 3: a test stub")

    assert ready(3) is False
    del not_built


def _imports_from(path: Path, module: str) -> set[str] | None:
    """The names a module imports from `steerai.loop2.<module>`; None when it imports the module
    itself (then any of its names could be reached through it)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    full = f"steerai.loop2.{module}"
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == full for a in node.names):
            return None
        if isinstance(node, ast.ImportFrom):
            if node.module == full:
                names.update(alias.name for alias in node.names)
            elif node.module == "steerai.loop2" and any(a.name == module for a in node.names):
                return None
    return names


STEP_4 = sorted(_registered())


@pytest.mark.parametrize("qualified", STEP_4)
def test_a_step_4_stub_is_unreachable_from_the_step_3_path(qualified: str) -> None:
    """By construction: no step 3 module imports the stub (nor, for a method, its class) or its
    module as a whole; the package has no dynamic imports (CLAUDE.md), so nothing else reaches
    it."""
    module, _, rest = qualified.removeprefix("steerai.loop2.").partition(".")
    owner = rest.split(".")[0]
    for path in STEP_3_PATH:
        imported = _imports_from(path, module)
        assert imported is not None, f"{path.name} imports {module} whole"
        assert owner not in imported, f"{path.name} imports {module}.{owner}"


def test_a_stub_reached_anyway_ends_the_task_with_an_internal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reached(*_args: object, **_kwargs: object) -> object:
        raise NotImplementedError("step 4: reached")

    monkeypatch.setattr(runner, "action_risk", reached)
    world = World([decision(act("click", target=GO.id), first=True)])
    outcome = run_task(task(), world.ports())
    assert (outcome.status, outcome.reason) == ("stopped", "internal_stub_reached")
    assert world.dispatched == []


def _pending_steps(path: Path) -> list[object]:
    """The `step=` of every `@pending(...)` decorator in a test module; None for one without."""
    steps: list[object] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.FunctionDef):
            continue
        for mark in node.decorator_list:
            if not (
                isinstance(mark, ast.Call)
                and isinstance(mark.func, ast.Name)
                and mark.func.id == "pending"
            ):
                continue
            given = [k.value for k in mark.keywords if k.arg == "step"]
            ok = len(given) == 1 and isinstance(given[0], ast.Constant)
            steps.append(given[0].value if ok and isinstance(given[0], ast.Constant) else None)
    return steps


def test_every_pending_mark_carries_its_step_and_none_is_step_3() -> None:
    steps = [step for path in TESTS.glob("test_*.py") for step in _pending_steps(path)]
    assert None not in steps, "every pending mark names its step"
    assert [s for s in steps if not (isinstance(s, int) and s >= 4)] == [], "a step >= 4"


def _ready_imports() -> set[str]:
    """The modules `ready()` imports, read from its source."""
    tree = ast.parse((LOOP2 / "__init__.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "steerai.loop2":
            names.update(alias.name for alias in node.names)
    return names


def test_ready_imports_every_module_of_the_package() -> None:
    """#355 LW-13: a stub in a module `ready()` never imported would never be counted."""
    modules = {p.stem for p in LOOP2.glob("*.py")} - {"__init__", "_stub"}
    assert _ready_imports() == modules


PACKAGE = ROOT / "steerai"
BUILDERS = {"Executor", "PhysicalExecutor", "ActPort"}


def _constructions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in BUILDERS
    }


def test_the_v2_executors_and_the_act_port_are_built_only_by_live_world() -> None:
    """#355 LW-04, the AST half: inside the v2 package only `live_world.py` builds v1's
    executors and the act port (so every one is guarded); outside it, v1 builds its own
    executors and nothing builds an `ActPort`."""
    offenders: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        built = _constructions(path)
        if LOOP2 in path.parents and path.name != "live_world.py" and built:
            offenders.append(f"{path.name}:{sorted(built)}")
        if LOOP2 not in path.parents and "ActPort" in built:
            offenders.append(f"{path.name}:ActPort")
    assert offenders == []
