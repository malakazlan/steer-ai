"""The stub registry ([V2.11] decision 8, packet §7 rev 4.0): every interface written before its
code is marked `@stub(step)` with the step that builds it, and `loop2.ready(step)` is False while
any mark of that step or an earlier one remains. A step removes each mark as it implements the
function."""

from __future__ import annotations

from collections.abc import Callable

PENDING: set[str] = set()
"""The qualified names of the interfaces still stubbed."""
PENDING_STEP: dict[str, int] = {}
"""Each stubbed interface's step."""


def stub[**P, R](step: int) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Register the decorated function as not built until `step`; it is returned unchanged."""
    if step not in {3, 4, 5, 6, 7}:
        raise ValueError("a stub's step is one of the V2 steps 3 to 7")

    def mark(function: Callable[P, R]) -> Callable[P, R]:
        name = f"{function.__module__}.{function.__qualname__}"
        PENDING.add(name)
        PENDING_STEP[name] = step
        return function

    return mark
