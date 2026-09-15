import pytest

from clamp import clamp, lerp


def test_clamp():
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10


def test_clamp_bad_bounds():
    with pytest.raises(ValueError):
        clamp(1, 5, 0)


def test_lerp():
    assert lerp(0, 10, 0.5) == 5
