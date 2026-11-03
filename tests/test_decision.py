"""Boundaries of the model-agnostic confidence and margin helpers."""

import pytest

from steerai.decision import peakedness, top2_margin


def test_peakedness_empty_is_zero() -> None:
    assert peakedness({}) == 0.0


def test_peakedness_single_option_is_fully_confident() -> None:
    assert peakedness({"3": 1.0}) == 1.0
    assert peakedness({"3": 0.2}) == 1.0


@pytest.mark.parametrize("k", [2, 3, 7])
def test_peakedness_uniform_is_zero(k: int) -> None:
    assert peakedness({str(i): 1.0 / k for i in range(k)}) == pytest.approx(0.0)


def test_peakedness_all_mass_on_one_of_two_is_one() -> None:
    assert peakedness({"3": 1.0, "2": 0.0}) == pytest.approx(1.0)


def test_peakedness_midpoint() -> None:
    assert peakedness({"3": 0.75, "2": 0.25}) == pytest.approx(0.5)


def test_peakedness_clamps_unnormalised_input_at_zero() -> None:
    assert peakedness({"3": 0.1, "2": 0.1}) == 0.0


def test_top2_margin_empty_is_zero() -> None:
    assert top2_margin({}) == 0.0


def test_top2_margin_single_option_is_its_mass() -> None:
    assert top2_margin({"3": 0.4}) == pytest.approx(0.4)


def test_top2_margin_ignores_insertion_order() -> None:
    assert top2_margin({"2": 0.1, "3": 0.6, "4": 0.3}) == pytest.approx(0.3)


def test_top2_margin_tie_is_zero() -> None:
    assert top2_margin({"3": 0.5, "none": 0.5}) == 0.0
