"""DPAPI (CryptProtectData / CryptUnprotectData): anything persisted with user data is encrypted
to the current Windows user; never a plaintext fallback."""

from __future__ import annotations

import sys

import pytest

from steerai.platform import dpapi

pytestmark = pytest.mark.windows


def test_round_trip_with_entropy() -> None:
    secret = b"before snapshot: Amir Khan \xc2\xb7 hi bro"
    blob = dpapi.protect(secret, entropy=b"steer-wal")
    assert blob != secret
    assert secret not in blob
    assert dpapi.unprotect(blob, entropy=b"steer-wal") == secret


def test_round_trip_without_entropy_and_empty_payload() -> None:
    assert dpapi.unprotect(dpapi.protect(b"")) == b""
    assert dpapi.unprotect(dpapi.protect(b"x" * 100_000)) == b"x" * 100_000


def test_wrong_entropy_is_refused() -> None:
    blob = dpapi.protect(b"payload", entropy=b"one")
    with pytest.raises(OSError, match="CryptUnprotectData"):
        dpapi.unprotect(blob, entropy=b"two")


def test_tampered_blob_is_refused() -> None:
    blob = bytearray(dpapi.protect(b"payload"))
    blob[len(blob) // 2] ^= 0xFF
    with pytest.raises(OSError, match="CryptUnprotectData"):
        dpapi.unprotect(bytes(blob))


def test_description_is_not_a_secret_channel() -> None:
    # The description is stored in clear by Windows; the API does not accept one.
    assert not hasattr(dpapi, "describe")


@pytest.mark.skipif(sys.platform == "win32", reason="the refusal is for other platforms")
def test_other_platforms_are_refused() -> None:  # pragma: no cover - Windows-only suite
    with pytest.raises(OSError, match="Windows"):
        dpapi.protect(b"x")
