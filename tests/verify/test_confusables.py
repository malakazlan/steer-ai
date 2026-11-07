"""The vendored lookalike tables agree with their Unicode data (5.13 1a [v2.26])."""

from __future__ import annotations

from pathlib import Path

from steerai.verify.confusables import (
    ASCII_LOOKALIKES,
    DEFAULT_IGNORABLE,
    UNICODE_VERSION,
    default_ignorable,
    generate_ascii_lookalikes,
    generate_default_ignorable,
    unicode_version,
)

DATA = Path(__file__).resolve().parents[1] / "data"


def test_the_ascii_lookalikes_are_the_datas() -> None:
    text = (DATA / "confusables.txt").read_text(encoding="utf-8-sig")
    assert generate_ascii_lookalikes(text) == ASCII_LOOKALIKES
    assert ASCII_LOOKALIKES == {"0": "o", "1": "l", "I": "l", "|": "l"}


def test_the_ignorable_ranges_are_the_datas_of_the_same_version() -> None:
    text = (DATA / "default_ignorable.txt").read_text(encoding="utf-8")
    assert unicode_version(text) == UNICODE_VERSION
    assert generate_default_ignorable(text) == DEFAULT_IGNORABLE


def test_default_ignorable_reads_the_ranges() -> None:
    for code in (0x00AD, 0x034F, 0x115F, 0x1160, 0x3164, 0xFE0F, 0xE0100, 0xE0FFF):
        assert default_ignorable(chr(code)), hex(code)
    for code in (0x0041, 0x00AC, 0x0350, 0x3165, 0xE1000):
        assert not default_ignorable(chr(code)), hex(code)
