"""The step 3c stub registry ([V2.11] decision 8): every interface written before its code is
marked `@stub`, and `loop2.ready()` is False while any mark remains. 3c removes each mark as it
implements the function; step 3 is done only when none is left."""

from __future__ import annotations

from collections.abc import Callable

PENDING: set[str] = set()
"""The qualified names of the interfaces still stubbed."""


def stub[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Register `function` as not built yet; it is returned unchanged."""
    PENDING.add(f"{function.__module__}.{function.__qualname__}")
    return function
