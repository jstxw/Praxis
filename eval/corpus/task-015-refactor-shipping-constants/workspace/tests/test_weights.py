import pytest

from weights import grams_to_ounces, total_grams


def test_grams_to_ounces():
    assert grams_to_ounces(28.349523125) == pytest.approx(1.0)


def test_total_grams():
    assert total_grams([(100, 2), (50, 3)]) == 350
    assert total_grams([]) == 0
