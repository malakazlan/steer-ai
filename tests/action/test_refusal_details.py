"""[V2.67] D: every `detail` an act result can carry is a token of one closed set, shared by the
executors (`execution.RefusalDetail`), the act port (`loop.PortDetail`) and the redacted turn
trace. The producers are read from their source: a detail written as a constant must be in the
set; a detail written as an expression must be one of the known dynamic sites below, each of
whose values is checked here. A new producer of either kind fails this test until the set
names it."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

from steerai.action.execution import REFUSAL_DETAILS
from steerai.action.tree import ExpandDecision
from steerai.loop2.act_guards import SECOND_REFUSED, SECOND_UNCERTAIN, SETTLED
from steerai.loop2.loop import PORT_DETAILS
from steerai.session.hook_classifier import TAKEOVER_TOKENS

ROOT = Path(__file__).resolve().parents[2] / "steerai"
EXECUTORS = ("action/execution.py", "action/executor.py", "action/physical.py", "action/tree.py")
PORT = ("loop2/act_port.py", "loop2/act_guards.py")
ALL = REFUSAL_DETAILS | PORT_DETAILS

DYNAMIC = {
    ("action/execution.py", "f'no_{name}'"),  # `pattern`: the pattern names, checked below
    ("action/execution.py", "token"),  # the blocker: the switch (`interrupted`)
    ("action/execution.py", "exc.detail"),  # a RefusalError's own detail, passed on
    ("action/executor.py", "detail"),  # `expand_decision`, `_option_refusal`, `_choose_result`
    ("action/executor.py", "refusal"),  # `_option_refusal`
    ("action/physical.py", "moved_detail"),  # `target_moved`
    ("action/physical.py", "moved_detail or 'covered'"),
    ("action/physical.py", "detail"),  # `hit_test_miss` or `covered_in_window`
    ("action/physical.py", "_unfocused_detail(snapshot, focused)"),
    ("action/physical.py", "reason"),  # `_burst_blocker`, or the switch
    ("loop2/act_guards.py", "token"),  # the switch
    ("loop2/act_guards.py", "self._takeover_token()"),
    ("loop2/act_guards.py", "stop.token"),  # the switch, through `kill_sleep`
    ("loop2/act_guards.py", "SECOND_UNCERTAIN"),
    ("loop2/act_guards.py", "SECOND_REFUSED"),
    ("loop2/act_guards.py", "SETTLED"),
    ("loop2/act_port.py", "detail"),  # `_not_sent`'s parameter; wall_budget or the takeover
    ("loop2/act_port.py", "refused.detail"),
    ("loop2/act_port.py", "tap"),  # `GuardDesk.tap_refusal`
    ("loop2/act_port.py", "self._desk.takeover_token()"),
    ("loop2/act_port.py", "SETTLED"),
    ("loop2/act_port.py", "SECOND_REFUSED"),
}
"""Every detail written as an expression, by file and source text."""


def _sites(rel: str) -> tuple[set[str], set[str]]:
    """A file's details: the constants, and the expressions, at every place a detail is
    written (a refusal's detail argument, a `"detail"` entry, an assignment to one)."""
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    spots: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            position = {"RefusalError": 1, "_Refused": 1, "_not_sent": 2, "_choose_result": 1}
            if name in position and len(node.args) > position[name]:
                spots.append(node.args[position[name]])
        if isinstance(node, ast.Dict):
            spots += [
                v
                for k, v in zip(node.keys, node.values, strict=True)
                if isinstance(k, ast.Constant) and k.value == "detail"
            ]
        if isinstance(node, ast.Assign):
            spots += [
                node.value
                for t in node.targets
                if isinstance(t, ast.Subscript)
                and isinstance(t.slice, ast.Constant)
                and t.slice.value == "detail"
            ]
    constants = {s.value for s in spots if isinstance(s, ast.Constant) and isinstance(s.value, str)}
    expressions = {ast.unparse(s) for s in spots if not isinstance(s, ast.Constant)}
    return constants, expressions


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
    found: set[str] = set()
    for rel in EXECUTORS:
        found |= _sites(rel)[0]
    assert found
    assert found <= REFUSAL_DETAILS, sorted(found - REFUSAL_DETAILS)


def test_every_constant_detail_of_the_act_port_is_in_the_shared_set() -> None:
    found: set[str] = set()
    for rel in PORT:
        found |= _sites(rel)[0]
    assert found
    assert found <= ALL, sorted(found - ALL)


def test_every_detail_written_as_an_expression_is_a_known_site() -> None:
    sites = {(rel, expr) for rel in EXECUTORS + PORT for expr in _sites(rel)[1]}
    assert sites == DYNAMIC, (sorted(sites - DYNAMIC), sorted(DYNAMIC - sites))


def test_each_missing_pattern_is_a_detail() -> None:
    """`UiaBindings.pattern` refuses with `no_<name>`, for every pattern an executor asks for."""
    names: set[str] = set()
    for rel in ("action/executor.py", "action/physical.py"):
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
