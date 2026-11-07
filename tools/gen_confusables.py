"""Regenerate the lookalike tables in `steerai/verify/confusables.py` from the vendored Unicode
data under `tests/data/`: the UTS #39 `confusables.txt` fold and ASCII lookalikes, and the
Default_Ignorable_Code_Point ranges extracted from `DerivedCoreProperties.txt`.

    uv run python tools/gen_confusables.py

The tables and the Unicode version are written between the generated markers; tests hold every
table to the generators' output on the same data, so this script and the tests agree by
construction. Both data files must name the same Unicode version.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "steerai" / "verify" / "confusables.py"
CONFUSABLES = ROOT / "tests" / "data" / "confusables.txt"
IGNORABLE = ROOT / "tests" / "data" / "default_ignorable.txt"
START = "# --- generated: do not edit; run `python tools/gen_confusables.py` ---\n"
END = "# --- end generated ---\n"


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from steerai.verify import confusables as gen  # noqa: PLC0415 - after sys.path

    confusables = CONFUSABLES.read_text(encoding="utf-8-sig")
    ignorable = IGNORABLE.read_text(encoding="utf-8")
    version = gen.unicode_version(confusables)
    if version is None or gen.unicode_version(ignorable) != version:
        sys.stderr.write("the data files do not name one Unicode version\n")
        return 1
    table = gen.generate_confusable_table(confusables)
    ascii_table = gen.generate_ascii_lookalikes(confusables)
    ranges = gen.generate_default_ignorable(ignorable)
    lines = ["CONFUSABLE_TO_LATIN: dict[str, str] = {"]
    lines.extend(f"    {source!a}: {target!r}," for source, target in table.items())
    lines.append("}")
    lines.append("ASCII_LOOKALIKES: dict[str, str] = {")
    lines.extend(f"    {source!r}: {target!r}," for source, target in ascii_table.items())
    lines.append("}")
    lines.append("DEFAULT_IGNORABLE: tuple[tuple[int, int], ...] = (")
    lines.extend(f"    (0x{low:04X}, 0x{high:04X})," for low, high in ranges)
    lines.append(")")
    block = START + "\n".join(lines) + "\n" + END
    source = TARGET.read_bytes().decode()
    begin = source.index(START)
    finish = source.index(END) + len(END)
    source = source[:begin] + block + source[finish:]
    source = re.sub(r'UNICODE_VERSION = "[^"]*"', f'UNICODE_VERSION = "{version}"', source)
    TARGET.write_bytes(source.encode())
    sys.stdout.write(
        f"{len(table)} fold entries, {len(ascii_table)} ASCII, {len(ranges)} ignorable ranges, "
        f"Unicode {version}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
