"""End-to-end T1 on a real Chrome page: type a message with the physical executor, press Send
through the semantic executor, and verify the new bubble against the before snapshot."""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from steerai.action.executor import Action, Executor
from steerai.action.physical import PhysicalAction, PhysicalExecutor
from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.win_input import acquire_foreground
from steerai.probes.windows import wait_window_for_pid
from steerai.verify.predicates import Appeared, Within
from steerai.verify.strength import Params, strength, template_for
from steerai.verify.t1 import label_of_record, verify
from steerai.world.elements import Element
from steerai.world.model import Snapshot, WorldModel

pytestmark = pytest.mark.windows
PAGE = Path(__file__).resolve().parents[2] / "steerai" / "probes" / "pages" / "inbox.html"
MESSAGE = "hello there from steer"


def _find(snapshot: Snapshot, role: str, name: str) -> Element:
    return next(e for e in snapshot.elements if e.role == role and e.name == name)


def _subtree_roles(snapshot: Snapshot, root: Element, name: str) -> list[str]:
    by_parent: dict[int | None, list[Element]] = {}
    for element in snapshot.elements:
        by_parent.setdefault(element.parent_id, []).append(element)
    roles: list[str] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.name == name and node is not root:
            roles.append(node.role)
        stack.extend(by_parent.get(node.id, []))
    return roles


@pytest.mark.skipif(not CHROME_DEFAULT.is_file(), reason="Chrome is not installed")
def test_send_on_the_inbox_page_is_verified_by_t1() -> None:
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-verify-"))
    # 1000x640 logical fits a 1080-pixel screen at 125 % DPI (measured 2026-11-04).
    proc = launch_chrome(CHROME_DEFAULT, profile_dir, 9335, PAGE.as_uri(), window_size=(1000, 640))
    try:
        window = wait_window_for_pid(proc.pid, title_contains="Google Chrome")
        acquire_foreground(window.hwnd)
        time.sleep(3.0)
        client = UiaClient()
        model = WorldModel(client)
        before = model.observe(window.hwnd, cap_s=20.0)
        composer = _find(before, "Edit", "Message")
        conversation = _find(before, "Group", "Conversation")
        content_roles = _subtree_roles(before, conversation, "ok")
        assert content_roles, "the conversation exposes its bubbles by name"
        content_role = content_roles[-1]

        physical = PhysicalExecutor()
        clicked = physical.execute(PhysicalAction(verb="click", element_id=composer.id), before)
        assert clicked.delivery.value.startswith("delivered"), clicked.to_dict()
        typed = physical.execute(
            PhysicalAction(verb="type", element_id=composer.id, text=MESSAGE), before
        )
        assert typed.ok is True, typed.to_dict()

        params = Params(
            target_id=_find(before, "Button", "Send").id,
            text=MESSAGE,
            composer_id=composer.id,
            scope=Within(landmark="Conversation"),
            content_role=content_role,
        )
        predicate = template_for("send", params)
        assert strength(predicate, "R3", params).ok is True

        pressed = Executor().execute(Action(verb="click", element_id=params.target_id or 0), before)
        assert pressed.delivery.value.startswith("delivered"), pressed.to_dict()
        after = model.observe(window.hwnd, cap_s=5.0, stable_window=1)
        started = time.perf_counter()
        verdict = verify(
            predicate,
            before,
            after,
            lambda **kw: model.observe(window.hwnd, cap_s=5.0, **kw),
            live=_ChromeLive(client),
        )
        elapsed = time.perf_counter() - started
        assert verdict.holds is True, (verdict, content_role, elapsed)
        assert verdict.level == 1
        label = label_of_record(
            verdict, predicate=predicate, action_key="k-send", before=before, after=after
        )
        assert label is not None
        assert label.holds is True
        assert MESSAGE not in str(label.to_dict())

        # The same send a second time against the same before snapshot is not a new effect.
        again = verify(
            Appeared(
                role=content_role, name_pattern=MESSAGE, within=Within(landmark="Conversation")
            ),
            after,
            model.observe(window.hwnd, cap_s=5.0, stable_window=1),
            lambda **kw: model.observe(window.hwnd, cap_s=5.0, **kw),
            deadline_s=0.5,
        )
        assert again.holds is False
        assert again.evidence["reason"] == "not_new"
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        shutil.rmtree(profile_dir, ignore_errors=True)


class _ChromeLive:
    """Live reads through the UIA client and the ValuePattern, for the composer-empty check."""

    def __init__(self, client: UiaClient) -> None:
        self._client = client

    def value(self, handle: Any) -> str | None:
        from steerai.action.execution import UIA_VALUE_PATTERN, UiaBindings  # noqa: PLC0415

        pattern = UiaBindings.load().pattern(handle, UIA_VALUE_PATTERN, "ValuePattern")
        raw = pattern.CurrentValue
        return None if raw is None else str(raw)

    def toggle_state(self, _handle: Any) -> int | None:
        return None

    def is_selected(self, _handle: Any) -> bool | None:
        return None

    def focused_runtime_id(self) -> tuple[int, ...]:
        return self._client.focused_runtime_id()
