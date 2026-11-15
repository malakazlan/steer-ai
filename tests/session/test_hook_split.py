"""[V2.60] hook helper, PR 1 (packet v2-hook-helper rev 2b §10.1): the pure split.

The helper process loads only the classifier, the probe ledger and the layout. None of them may
reach input-sending code (`steerai.action`, `steerai.probes`) or the in-process monitor, at any
depth (S6). The split changes no behaviour: `MonitorCore` is their composition and its own tests
pass unchanged."""

from __future__ import annotations

import ast
import subprocess
import sys
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


def _path(root: Path, module: str) -> Path:
    base = root.joinpath(*module.split("."))
    package = base / "__init__.py"
    return package if package.is_file() else base.with_suffix(".py")


def _imported_names(root: Path, module: str) -> set[str]:
    """Every name `module` imports, absolute: relative imports resolved against its package,
    at module level or inside a function."""
    path = _path(root, module)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                if node.level - 1 >= len(parts):
                    raise AssertionError(f"{module}: a relative import above the root")
                anchor = ".".join(parts[: len(parts) - (node.level - 1)])
                base = f"{anchor}.{base}" if base else anchor
            found.add(base)
            found.update(f"{base}.{a.name}" for a in node.names)
    return found


def _closure(module: str, root: Path = ROOT) -> set[str]:
    """Every name reached from `module`: its imports, theirs, and every package `__init__` on
    the way, the root package's included. Names are kept before any is-it-a-file filter, so an
    imported attribute of a forbidden module is caught too."""
    names: set[str] = set()
    seen: set[str] = set()
    todo = [module]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        names.add(name)
        parts = name.split(".")
        todo.extend(
            p for p in (".".join(parts[:i]) for i in range(1, len(parts)))
            if _path(root, p).is_file()
        )  # fmt: skip
        imported = _imported_names(root, name)
        names.update(imported)
        todo.extend(
            n for n in imported if n.split(".")[0] == "steerai" and _path(root, n).is_file()
        )
    return names


@pytest.mark.parametrize("module", HELPER_MODULES)
def test_the_helpers_modules_never_reach_input_sending_code(module: str) -> None:
    """S6: the helper never sends input, so nothing it loads can."""
    reached = _closure(module)
    assert not [m for m in reached if m.startswith(FORBIDDEN)], sorted(reached)


def test_the_closure_walker_does_see_a_forbidden_import() -> None:
    """The walker itself: the in-process monitor reaches input-sending code."""
    assert any(m.startswith("steerai.probes") for m in _closure("steerai.session.monitor"))


def _tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


_CLEAN = {
    "steerai/__init__.py": "",
    "steerai/session/__init__.py": "",
    "steerai/session/hook_layout.py": "",
    "steerai/probes/__init__.py": "",
    "steerai/probes/win_input.py": "",
}


@pytest.mark.parametrize(
    ("where", "text"),
    [
        ("steerai/session/hook_layout.py", "from ..probes import win_input"),
        ("steerai/session/hook_layout.py", "from .. import probes"),
        ("steerai/session/hook_layout.py", "def f() -> None:\n    from ..probes import win_input"),
        ("steerai/__init__.py", "from steerai.probes import win_input"),
        ("steerai/session/__init__.py", "from steerai.probes.win_input import STEER_EXTRA_INFO"),
    ],
)
def test_the_walker_catches_relative_imports_and_package_inits(
    tmp_path: Path, where: str, text: str
) -> None:
    """#373 review: a relative import and the root package's `__init__` were blind spots."""
    _tree(tmp_path, {**_CLEAN, where: text})
    reached = _closure("steerai.session.hook_layout", tmp_path)
    assert [m for m in reached if m.startswith(FORBIDDEN)], sorted(reached)


def test_a_clean_tree_reaches_nothing_forbidden(tmp_path: Path) -> None:
    _tree(tmp_path, _CLEAN)
    reached = _closure("steerai.session.hook_layout", tmp_path)
    assert not [m for m in reached if m.startswith(FORBIDDEN)], sorted(reached)


def test_importing_the_helpers_modules_loads_nothing_forbidden() -> None:
    """#373 review: what actually loads, dynamic imports included, in a fresh isolated
    interpreter."""
    code = (
        "import sys\n"
        + "".join(f"import {m}\n" for m in HELPER_MODULES)
        + "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('steerai'))))\n"
    )
    done = subprocess.run(  # noqa: S603 - a fixed argument list, no shell
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    loaded = done.stdout.split()
    assert set(HELPER_MODULES) <= set(loaded)
    assert not [m for m in loaded if m.startswith(FORBIDDEN)], loaded


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
