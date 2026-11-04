"""TK-19/37: a hundred thousand events leave the core with counters and one instant, nothing
that grows with the input (a 1-8 kHz mouse over a 60 s wait is 480k events)."""

from __future__ import annotations

from steerai.session.monitor import LLMHF_INJECTED, WM_MOUSEMOVE, WM_MOUSEWHEEL, MonitorCore

EVENTS = 100_000
KINDS = 5  # user, injected_other, steer, answer, callback_errors


def test_a_hundred_thousand_events_leave_counters_and_one_instant_only() -> None:
    core = MonitorCore(started_at=0.0)
    for i in range(EVENTS):
        if i % 3 == 0:
            core.on_keyboard(flags=0, extra=0, now=float(i))
        elif i % 3 == 1:
            core.on_mouse(message=WM_MOUSEMOVE, flags=0, extra=0, pt=(i * 10, 0), now=float(i))
        else:
            core.on_mouse(
                message=WM_MOUSEWHEEL, flags=LLMHF_INJECTED, extra=0, pt=(0, 0), now=float(i)
            )
    counts = core.counts()
    assert sum(counts.values()) == EVENTS
    assert len(counts) <= KINDS
    for name, value in vars(core).items():
        if isinstance(value, (list, set, dict, tuple)):
            assert name in ("_counts", "_reference"), f"{name} accumulates"
            assert len(value) <= KINDS, f"{name} grew to {len(value)}"
    assert core.last_user_input() == float(EVENTS - 1)
