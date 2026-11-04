"""Regenerate the confusable fold table in `steerai/world/people.py` from Unicode UTS #39
`confusables.txt` (the vendored copy under `tests/data/`, or a path given on the command line).

    uv run python tools/gen_confusables.py [path/to/confusables.txt]

The table and the Unicode version are written between the generated markers; a test holds the
vendored table to the generator's output, so this script and the test agree by construction.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "steerai" / "world" / "people.py"
DATA = ROOT / "tests" / "data" / "confusables.txt"
START = "# --- generated: do not edit; run `python tools/gen_confusables.py` ---\n"
END = "# --- end generated ---\n"


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(ROOT))
    from steerai.world.people import generate_confusable_table  # noqa: PLC0415 - after sys.path

    data_path = Path(argv[1]) if len(argv) > 1 else DATA
    text = data_path.read_text(encoding="utf-8-sig")
    version = re.search(r"^# Version: (\S+)", text, re.MULTILINE)
    if version is None:
        sys.stderr.write("no Version line in the data file\n")
        return 1
    table = generate_confusable_table(text)
    lines = ["CONFUSABLE_TO_LATIN: dict[str, str] = {"]
    lines.extend(f"    {source!a}: {target!r}," for source, target in table.items())
    lines.append("}")
    block = START + "\n".join(lines) + "\n" + END
    source = TARGET.read_bytes().decode()
    begin = source.index(START)
    finish = source.index(END) + len(END)
    source = source[:begin] + block + source[finish:]
    source = re.sub(r'UNICODE_VERSION = "[^"]*"', f'UNICODE_VERSION = "{version.group(1)}"', source)
    TARGET.write_bytes(source.encode())
    sys.stdout.write(f"{len(table)} entries, Unicode {version.group(1)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
