import pytest

from safe_math import safe_divide, safe_int


def test_safe_int_unchanged():
    assert safe_int("12") == 12
    assert safe_int("x", default=-1) == -1


def test_normal_division():
    assert safe_divide(6, 3) == 2
    assert safe_divide(1, 4) == 0.25


def test_divide_by_zero_returns_none_by_default():
    assert safe_divide(1, 0) is None


def test_divide_by_zero_returns_custom_default():
    assert safe_divide(1, 0, default=0) == 0
    assert safe_divide(1, 0, default="n/a") == "n/a"


def test_divide_by_float_zero():
    assert safe_divide(1.5, 0.0, default=-1) == -1
    assert safe_divide(1.5, -0.0, default=-1) == -1


def test_zero_numerator_is_fine():
    assert safe_divide(0, 5) == 0


def test_type_errors_still_propagate():
    with pytest.raises(TypeError):
        safe_divide("6", 2)


def test_type_error_with_zero_divisor_still_propagates():
    with pytest.raises(TypeError):
        safe_divide("6", 0)


def test_falsy_default_is_respected():
    assert safe_divide(3, 0, default=0.0) == 0.0
    assert safe_divide(3, 0, default=False) is False
