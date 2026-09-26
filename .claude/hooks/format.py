"""PostToolUse hook for Edit|Write: format a Python file with ruff after it is written.

Only formats; never auto-fixes lint, so real problems stay visible in `ruff check`.
Silent when the file is not Python or the project environment is not synced yet.
"""

import json
import pathlib
import shutil
import subprocess
import sys


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return
    path = str(payload.get("tool_input", {}).get("file_path", ""))
    target = pathlib.Path(path)
    if target.suffix != ".py" or not target.is_file():
        return
    uv = shutil.which("uv")
    venv_bin = pathlib.Path(".venv") / ("Scripts" if sys.platform == "win32" else "bin")
    if uv is None or not venv_bin.is_dir():
        return
    subprocess.run(  # noqa: S603 - fixed argv; path is an existing .py file from the hook payload
        [uv, "run", "--no-sync", "ruff", "format", "--quiet", str(target)],
        timeout=25,
        check=False,
    )


if __name__ == "__main__":
    main()
