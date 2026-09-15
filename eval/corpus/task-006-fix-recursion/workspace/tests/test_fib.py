import pytest

from fib import fib


def test_fib_small():
    assert [fib(i) for i in range(8)] == [0, 1, 1, 2, 3, 5, 8, 13]


def test_fib_negative():
    with pytest.raises(ValueError):
        fib(-1)
