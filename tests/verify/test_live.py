"""End-to-end T1 on a real Chrome page: type a message with the physical executor, press Send
through the semantic executor, and verify the new bubble against the before snapshot."""

from __future__ import annotations

import contextlib
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from steerai.action.execution import UiaBindings
from steerai.action.executor import Action, Executor
from steerai.action.live_reads import UiaLiveReads
from steerai.action.physical import PhysicalAction, PhysicalExecutor
from steerai.errors import ErrorCode
from steerai.probes.chrome_a11y import CHROME_DEFAULT, launch_chrome, stop_chrome
from steerai.probes.uia_snapshot import UiaClient
from steerai.probes.win_input import acquire_foreground
from steerai.probes.windows import wait_window_for_pid
from steerai.verify.predicates import Appeared, ValueEquals, Within
from steerai.verify.strength import Params, strength, template_for
from steerai.verify.t1 import label_of_record, verify
from steerai.world.elements import Element
from steerai.world.model import Snapshot, WorldModel

pytestmark = pytest.mark.windows
PAGE = Path(__file__).resolve().parents[2] / "steerai" / "probes" / "pages" / "inbox.html"
MESSAGE = "hello there from steer"


def _observe_until_exposed(model: WorldModel, hwnd: int, *, max_s: float = 25.0) -> Snapshot:
    """Chromium's first-contact tree is thin and reads as stable (probe 5 ledger: cold settle
    10-25 s); observe until the composer is exposed or `max_s` elapse."""
    deadline = time.monotonic() + max_s
    while True:
        snapshot = model.observe(hwnd, cap_s=5.0)
        if any(e.role == "Edit" and e.name == "Message" for e in snapshot.elements):
            return snapshot
        if time.monotonic() >= deadline:
            raise AssertionError(f"composer never exposed; {snapshot.node_count} nodes")
        time.sleep(0.5)


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
@pytest.mark.usefixtures("warm_chrome")  # the first Chrome start, measured (#39)
def test_send_on_the_inbox_page_is_verified_by_t1() -> None:
    profile_dir = Path(tempfile.mkdtemp(prefix="steer-verify-"))
    # 1000x640 logical fits a 1080-pixel screen at 125 % DPI (measured 2026-11-04).
    proc = launch_chrome(CHROME_DEFAULT, profile_dir, 9335, PAGE.as_uri(), window_size=(1000, 640))
    try:
        window = wait_window_for_pid(
            proc.pid, title_contains="Google Chrome", image="chrome.exe", process=proc
        )
        acquire_foreground(window.hwnd)
        client = UiaClient()
        model = WorldModel(client)
        before = _observe_until_exposed(model, window.hwnd)
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

        # Against a before snapshot that already shows the bubble, the same text is not a new
        # effect. (The orchestrator's `after` may predate the DOM update, which is why the
        # verdict names the snapshot that decided it; a fresh settled snapshot is used here.)
        settled = model.observe(window.hwnd, cap_s=5.0)
        again = verify(
            Appeared(
                role=content_role, name_pattern=MESSAGE, within=Within(landmark="Conversation")
            ),
            settled,
            model.observe(window.hwnd, cap_s=5.0, stable_window=1),
            lambda **kw: model.observe(window.hwnd, cap_s=5.0, **kw),
            deadline_s=0.5,
        )
        assert again.holds is False
        assert again.evidence["reason"] == "not_new"
    finally:
        stop_chrome(proc, profile_dir)


def test_value_equals_on_notepad_fails_on_an_empty_document_and_passes_after_set_value() -> None:
    """The two deterministic `ValueEquals` paths on a native control: a fresh document is empty,
    so a text that never landed is a settled contradiction after one poll; after `set_value`
    the same predicate passes on the caller's after snapshot without a poll."""
    proc = subprocess.Popen(["notepad.exe"])  # noqa: S607 - fixed, well-known executable
    try:
        window = wait_window_for_pid(proc.pid)
        time.sleep(0.5)
        client = UiaClient()
        model = WorldModel(client)
        live = UiaLiveReads(client, UiaBindings.load())

        def observe(**kwargs: Any) -> Snapshot:
            return model.observe(window.hwnd, cap_s=5.0, **kwargs)

        before = observe()
        target = next(e for e in before.elements if e.role in ("Document", "Edit"))
        never = ValueEquals(target_id=target.id, text="never typed")
        empty = verify(never, before, observe(stable_window=1), observe, live=live)
        assert empty.holds is False, empty
        assert empty.code is ErrorCode.VERIFY_FAILED
        assert empty.evidence["reason"] == "value_empty"
        assert empty.polls == 1

        typed = ValueEquals(target_id=target.id, text="steer t1 pass")
        result = Executor().execute(
            Action(verb="set_value", element_id=target.id, text="steer t1 pass"), before
        )
        assert result.ok is True, result.to_dict()
        after = observe(stable_window=1)
        passed = verify(typed, before, after, observe, live=live)
        assert passed.holds is True, passed
        assert passed.code is None
        assert passed.polls == 0
        label = label_of_record(
            passed, predicate=typed, action_key="k-type", before=before, after=after
        )
        assert label is not None
        assert label.holds is True
        assert label.after_id == after.id
        assert "steer t1 pass" not in str(label.to_dict())
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


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

    def expand_state(self, _handle: Any) -> int | None:
        return None

    def focused_runtime_id(self) -> tuple[int, ...]:
        return self._client.focused_runtime_id()
