import pytest

from discounts import apply_percent_off


def test_apply_percent_off():
    assert apply_percent_off(1000, 25) == 750
    assert apply_percent_off(999, 10) == 899


def test_apply_percent_off_bounds():
    assert apply_percent_off(500, 0) == 500
    assert apply_percent_off(500, 100) == 0
    with pytest.raises(ValueError):
        apply_percent_off(500, 101)
