"""`python -m steerai.app`: open the launcher (design 5.1 asks surface)."""

from __future__ import annotations

import os
import sys
import tkinter as tk
from pathlib import Path


def main() -> int:
    from steerai.app.overlay import Overlay  # noqa: PLC0415 - Tk and Win32 only when launched
    from steerai.probes.windows import console_exclusions, list_top_windows  # noqa: PLC0415
    from steerai.run import real_deps  # noqa: PLC0415

    base = Path(os.environ.get("LOCALAPPDATA", ".")) / "steer"
    root = tk.Tk()
    Overlay(
        root=root,
        deps=real_deps(),
        list_windows=list_top_windows,
        exclusions=console_exclusions,
        ledger_dir=base / "ledger",
        trace_dir=base / "traces",
    )
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
