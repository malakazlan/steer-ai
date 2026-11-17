"""[V2.80]: what a Windows test must not leave behind. A hook, the kill hot key and the monitor
thread are one thing: `HookMonitor` installs and removes all of them on its own thread
(`steer-monitor`), so a live one is a leaked hook and hot key; a `steer-loss-watch` thread that
is still alive is a monitor held open past its test."""

from __future__ import annotations

import threading

LEAK_THREAD_NAMES = ("steer-monitor", "steer-loss-watch")
JOIN_S = 3.0
"""How long a thread that is already ending may take (the monitor's own close is bounded at 2 s)."""


def leaked_threads() -> list[str]:
    """The names of the monitor or watch threads still alive after their join window."""
    leaked: list[str] = []
    for thread in threading.enumerate():
        if thread.name in LEAK_THREAD_NAMES and thread is not threading.current_thread():
            thread.join(timeout=JOIN_S)
            if thread.is_alive():
                leaked.append(thread.name)
    return leaked
