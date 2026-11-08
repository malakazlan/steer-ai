"""Secure is sticky for the whole task (widget-verbs WV-09, W2 verdict item 3): a field once seen
as a password field stays `secure` by its RuntimeId, so a "Show password" box never makes its
value readable to listings, live reads or traces.

Measured (`steerai.probes.widget_patterns --section secure`, Chrome 156, n=5): ticking "Show
password" keeps the field's RuntimeId, and the snapshot then reads it not secure. RuntimeIds are
reusable over time (Microsoft docs), so a reused id is marked secure too: that fails closed.
"""

from __future__ import annotations

from dataclasses import replace

from steerai.world.model import Snapshot


class SecureMemory:
    """The RuntimeIds seen secure in one task; `apply` marks them secure in a later snapshot."""

    def __init__(self) -> None:
        self._seen: set[tuple[int, ...]] = set()

    def apply(self, snapshot: Snapshot) -> Snapshot:
        self._seen.update(e.runtime_id for e in snapshot.elements if e.secure and e.runtime_id)
        if not any(not e.secure and e.runtime_id in self._seen for e in snapshot.elements):
            return snapshot
        elements = [
            replace(e, secure=True) if not e.secure and e.runtime_id in self._seen else e
            for e in snapshot.elements
        ]
        return replace(snapshot, elements=elements)
