"""DPAPI: encrypt bytes to the current Windows user (`CryptProtectData`) and back
(`CryptUnprotectData`), crypt32.dll. Used for every persisted blob that holds user data (the
write-ahead record, traces, the cache; CLAUDE.md "Secure"). There is no plaintext fallback: on
any other platform, or when the call fails, the caller gets `OSError` and must not persist.

Reference: learn.microsoft.com/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata. The output
blob is owned by the caller and must be released with `LocalFree`; `CRYPTPROTECT_UI_FORBIDDEN`
keeps DPAPI from ever showing a prompt. The optional description Windows stores in clear is not
exposed, so no name can leak through it.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DATA_BLOB(ctypes.Structure):  # noqa: N801 - Win32 name
    _fields_ = (("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char)))


def _blob(data: bytes) -> _DATA_BLOB:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _libraries() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    if sys.platform != "win32":
        raise OSError("DPAPI requires Windows; nothing with user data is persisted elsewhere")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_pointer = ctypes.POINTER(_DATA_BLOB)
    for name in ("CryptProtectData", "CryptUnprotectData"):
        function = getattr(crypt32, name)
        function.argtypes = (
            blob_pointer,
            wintypes.LPCWSTR,
            blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            blob_pointer,
        )
        function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL
    return crypt32, kernel32


def _call(name: str, data: bytes, entropy: bytes | None) -> bytes:
    crypt32, kernel32 = _libraries()
    source = _blob(data)
    salt = _blob(entropy) if entropy is not None else None
    out = _DATA_BLOB()
    ok = getattr(crypt32, name)(
        ctypes.byref(source),
        None,
        ctypes.byref(salt) if salt is not None else None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    )
    if not ok:
        raise OSError(f"{name} failed: error {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(data: bytes, *, entropy: bytes | None = None) -> bytes:
    """Encrypt `data` to the current user; `entropy` is a caller secret both sides must know."""
    return _call("CryptProtectData", data, entropy)


def unprotect(blob: bytes, *, entropy: bytes | None = None) -> bytes:
    """Decrypt a blob made by `protect` on this user account with the same entropy."""
    return _call("CryptUnprotectData", blob, entropy)
