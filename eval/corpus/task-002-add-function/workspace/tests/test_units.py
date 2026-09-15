import pytest

from units import c_to_f, km_to_miles


def test_c_to_f():
    assert c_to_f(0) == 32
    assert c_to_f(100) == 212


def test_km_to_miles():
    assert km_to_miles(10) == pytest.approx(6.21371)
