"""[V2.67] D, #444: every `detail` an act result can carry is a token of one closed set, shared
by the executors (`execution.RefusalDetail`), the act port (`loop.PortDetail`) and the redacted
turn trace. The producers are read from their source, across all of `steerai/`: every file that
builds an `ActionResult` or calls a refusal producer is either on the act path (the modules of
the classes the act port drives) or named below as outside it, with a reason. On the act path a
detail written as a constant must be in the set; one written as an expression must be a known
site, keyed by (file, function, text) with its count. A new producer, a new file, or a site that
moves to another function fails this test until it is named."""

from __future__ import annotations

import ast
import inspect
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import get_args

from steerai.action import tree
from steerai.action.execution import REFUSAL_DETAILS, UiaBindings
from steerai.action.executor import Executor
from steerai.action.physical import PhysicalExecutor
from steerai.action.tree import ExpandDecision
from steerai.loop2.act_guards import SECOND_REFUSED, SECOND_UNCERTAIN, SETTLED, InputGuard
from steerai.loop2.act_port import ActPort
from steerai.loop2.loop import PORT_DETAILS
from steerai.session.hook_classifier import TAKEOVER_TOKENS

ROOT = Path(__file__).resolve().parents[2] / "steerai"
ALL = REFUSAL_DETAILS | PORT_DETAILS
PRODUCERS = {"RefusalError": 1, "_Refused": 1, "_not_sent": 2, "_choose_result": 1}
"""Each refusal producer and the position of its detail argument."""


def _rel(obj: type | ModuleType) -> str:
    path = inspect.getsourcefile(obj)
    assert path is not None
    return Path(path).resolve().relative_to(ROOT).as_posix()


EXECUTORS = frozenset(_rel(o) for o in (UiaBindings, Executor, PhysicalExecutor, tree))
"""The executors the act port drives, by the files their classes live in."""
PORT = frozenset(_rel(o) for o in (ActPort, InputGuard))
ACT_PATH = EXECUTORS | PORT
OUTSIDE_ACT_PATH = {
    "action/launcher.py": "the `open` verb's launcher; the act port refuses `open` in step 3",
    "orchestrator/loop.py": "v1's Session: its step evidence, never an act port result",
    "probes/layer_b.py": "a probe's own recorded result, never sent through the act port",
}
"""Files that build an act result but are not on the v2 act path, each with why."""

DYNAMIC = Counter(
    {
        ("action/execution.py", "Call.send", "token"): 1,  # the blocker: the switch
        ("action/execution.py", "UiaBindings.pattern", "f'no_{name}'"): 1,  # checked below
        ("action/execution.py", "code_for", "exc.detail"): 1,  # a RefusalError's own detail
        ("action/executor.py", "Executor._choose", "detail"): 1,  # `_option_refusal`
        ("action/executor.py", "Executor._tree_expand", "detail"): 1,  # `expand_decision`
        ("action/executor.py", "_choose_result", "detail"): 1,
        ("action/executor.py", "_chosen", "refusal"): 1,  # `_option_refusal`
        ("action/physical.py", "PhysicalExecutor._hit_test", "detail"): 1,
        ("action/physical.py", "PhysicalExecutor._hit_test", "moved_detail"): 1,
        ("action/physical.py", "PhysicalExecutor._hit_test", "moved_detail or 'covered'"): 1,
        ("action/physical.py", "PhysicalExecutor._press", "_unfocused_detail(snapshot, focused)"):
            1,
        ("action/physical.py", "PhysicalExecutor._stopped", "reason"): 2,  # the switch, a burst
        ("loop2/act_guards.py", "InputGuard._fresh_idle", "self._takeover_token()"): 2,
        ("loop2/act_guards.py", "InputGuard._keyboard", "self._takeover_token()"): 1,
        ("loop2/act_guards.py", "InputGuard._switch", "token"): 1,
        ("loop2/act_guards.py", "InputGuard.double_click_time_s", "stop.token"): 1,
        ("loop2/act_guards.py", "_OneSubstitution.Toggle", "SECOND_UNCERTAIN"): 1,
        ("loop2/act_guards.py", "_OneSubstitution._second", "SECOND_REFUSED"): 2,
        ("loop2/act_guards.py", "_OneSubstitution._second", "SETTLED"): 1,
        ("loop2/act_port.py", "ActPort._boundary", "tap"): 1,  # `GuardDesk.tap_refusal`
        ("loop2/act_port.py", "ActPort._physical_send", "detail"): 1,
        ("loop2/act_port.py", "ActPort._proven_just_now", "self._desk.takeover_token()"): 1,
        ("loop2/act_port.py", "ActPort._wait_for_idle", "detail"): 1,  # wall_budget, takeover
        ("loop2/act_port.py", "ActPort.act", "refused.detail"): 1,
        ("loop2/act_port.py", "_not_sent", "detail"): 1,
        ("loop2/act_port.py", "_one_toggle", "SECOND_REFUSED"): 1,
        ("loop2/act_port.py", "_one_toggle", "SETTLED"): 1,
    }
)  # fmt: skip
"""Every act-path detail written as an expression: (file, function, source text) and how many
times it is written there."""

Site = tuple[str, str, ast.expr]
"""A detail written: its file, its enclosing function (`Class.method`), and the expression."""


def _builds_results(tree_: ast.Module) -> bool:
    return any(
        isinstance(n, ast.Call) and _name(n.func) in {"ActionResult", *PRODUCERS}
        for n in ast.walk(tree_)
    )


def _name(func: ast.expr) -> str:
    return str(getattr(func, "id", getattr(func, "attr", "")))


def _sites(rel: str) -> list[Site]:
    """A file's details at every place one is written (a producer's detail argument, a
    `"detail"` entry, an assignment to one), each with its enclosing function."""
    found: list[Site] = []

    def visit(node: ast.AST, where: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = where
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                inner = f"{where}.{child.name}" if where else child.name
            if isinstance(child, ast.Call):
                at = PRODUCERS.get(_name(child.func))
                if at is not None and len(child.args) > at:
                    found.append((rel, where or "<module>", child.args[at]))
            if isinstance(child, ast.Dict):
                found.extend(
                    (rel, where or "<module>", v)
                    for k, v in zip(child.keys, child.values, strict=True)
                    if isinstance(k, ast.Constant) and k.value == "detail"
                )
            if isinstance(child, ast.Assign):
                found.extend(
                    (rel, where or "<module>", child.value)
                    for t in child.targets
                    if isinstance(t, ast.Subscript)
                    and isinstance(t.slice, ast.Constant)
                    and t.slice.value == "detail"
                )
            visit(child, inner)

    visit(ast.parse((ROOT / rel).read_text(encoding="utf-8")), "")
    return found


def _constants(files: frozenset[str]) -> set[str]:
    return {
        expr.value
        for rel in files
        for _file, _where, expr in _sites(rel)
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str)
    }


def test_every_file_that_builds_an_act_result_is_on_the_act_path_or_named() -> None:
    """All of `steerai/` is read: a new file that builds one fails until it is placed."""
    builders = {
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*.py")
        if _builds_results(ast.parse(path.read_text(encoding="utf-8")))
    }
    assert builders == ACT_PATH | set(OUTSIDE_ACT_PATH), (
        sorted(builders - ACT_PATH - set(OUTSIDE_ACT_PATH)),
        sorted((ACT_PATH | set(OUTSIDE_ACT_PATH)) - builders),
    )
    assert not ACT_PATH & set(OUTSIDE_ACT_PATH)


def _values(expr: ast.expr) -> set[str]:
    """The strings an expression can be: a constant, either branch of a conditional, either
    side of an `or` (never a constant it only compares against)."""
    if isinstance(expr, ast.Constant):
        return {expr.value} if isinstance(expr.value, str) else set()
    if isinstance(expr, ast.IfExp):
        return _values(expr.body) | _values(expr.orelse)
    if isinstance(expr, ast.BoolOp):
        return {v for side in expr.values for v in _values(side)}
    return set()


def _returns(rel: str, *functions: str) -> set[str]:
    """The strings the named functions return."""
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    return {
        value
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef) and fn.name in functions
        for node in ast.walk(fn)
        if isinstance(node, ast.Return) and node.value is not None
        for value in _values(node.value)
    }


def test_every_constant_detail_of_an_executor_is_in_the_executors_set() -> None:
    found = _constants(EXECUTORS)
    assert found
    assert found <= REFUSAL_DETAILS, sorted(found - REFUSAL_DETAILS)


def test_every_constant_detail_of_the_act_port_is_in_the_shared_set() -> None:
    found = _constants(PORT)
    assert found
    assert found <= ALL, sorted(found - ALL)


def test_every_detail_written_as_an_expression_is_a_known_site_with_its_count() -> None:
    sites = Counter(
        (rel, where, ast.unparse(expr))
        for path in ACT_PATH
        for rel, where, expr in _sites(path)
        if not isinstance(expr, ast.Constant)
    )
    assert sites == DYNAMIC, (sorted((sites - DYNAMIC).items()), sorted((DYNAMIC - sites).items()))


def test_each_missing_pattern_is_a_detail() -> None:
    """`UiaBindings.pattern` refuses with `no_<name>`, for every pattern an executor asks for."""
    names: set[str] = set()
    for rel in sorted(EXECUTORS):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "pattern":
                _handle, _pattern_id, name = node.args  # every call names its pattern
                if isinstance(name, ast.Constant) and isinstance(name.value, str):
                    names.add(name.value)
    assert names
    assert {f"no_{n}" for n in names} <= REFUSAL_DETAILS


def test_each_dynamic_detail_value_is_in_the_shared_set() -> None:
    expand = (set(get_args(ExpandDecision)) - {"click"}) | {"expand_by_click"}
    values = (
        expand
        | _returns("action/executor.py", "_option_refusal")
        | {"no_options"}
        | _returns("action/physical.py", "_unfocused_detail", "_burst_blocker", "tap_refusal")
        | {"target_moved", "covered", "hit_test_miss", "covered_in_window"}
        | _returns("loop2/act_guards.py", "tap_refusal")
        | _returns("loop2/live_world.py", "interrupted", "_kill_token", "_session_signal")
        | _returns("session/helper_monitor.py", "session_change")
        | set(TAKEOVER_TOKENS)
        | {SECOND_REFUSED, SECOND_UNCERTAIN, SETTLED, "wall_budget", "generation"}
        | {"target_not_focused", "press_not_at_focus"}
    )
    assert values <= ALL, sorted(values - ALL)


def test_the_two_sets_name_no_token_twice() -> None:
    assert not REFUSAL_DETAILS & PORT_DETAILS, sorted(REFUSAL_DETAILS & PORT_DETAILS)
