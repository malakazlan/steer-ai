"""A live value read that fails in COM is a taxonomy failure, never a crash (field-value ruling 4,
hunter FV-16): `code_for` maps it; a programming error still raises."""

from __future__ import annotations

from typing import Any

import pytest

from steerai.action.live_reads import UiaLiveReads
from steerai.errors import ErrorCode, SteerError


class _ComError(Exception):
    """A stand-in for `_ctypes.COMError`: an exception carrying an HRESULT."""

    def __init__(self, hresult: int) -> None:
        super().__init__("COM failure")
        self.hresult = hresult


class _Bindings:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def pattern(self, _handle: Any, _pattern_id: int, _name: str) -> Any:
        raise self._error


class _Client:
    def focused_runtime_id(self) -> tuple[int, ...]:
        return ()


def test_a_com_failure_in_a_value_read_is_a_steer_error() -> None:
    reads = UiaLiveReads(_Client(), _Bindings(_ComError(-2147220991)))  # UIA_E_ELEMENTNOTAVAILABLE
    with pytest.raises(SteerError) as caught:
        reads.value(object())
    assert caught.value.message == "live_read_failed"
    assert isinstance(caught.value.code, ErrorCode)


def test_a_programming_error_in_a_value_read_still_raises() -> None:
    reads = UiaLiveReads(_Client(), _Bindings(KeyError("bug")))
    with pytest.raises(KeyError):
        reads.value(object())


@pytest.mark.parametrize(
    ("hresult", "code"),
    [
        (-2147220991, ErrorCode.ELEMENT_NOT_FOUND),  # UIA_E_ELEMENTNOTAVAILABLE: a stale handle
        (-2147023174, ErrorCode.APP_CRASHED),  # RPC_S_SERVER_UNAVAILABLE: the provider died
        (-2147467259, ErrorCode.PERCEPTION_TIMEOUT),  # E_FAIL: unmapped
    ],
)
def test_a_com_failure_keeps_the_code_code_for_gives_it(hresult: int, code: ErrorCode) -> None:
    """Ruling 4: mapped through `code_for`, never one fixed code."""
    reads = UiaLiveReads(_Client(), _Bindings(_ComError(hresult)))
    with pytest.raises(SteerError) as caught:
        reads.value(object())
    assert caught.value.code is code
