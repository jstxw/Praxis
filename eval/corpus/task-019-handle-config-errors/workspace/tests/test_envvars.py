import pytest

from envvars import to_bool


def test_to_bool_values():
    assert to_bool(" Yes ") is True
    assert to_bool("off") is False


def test_to_bool_invalid():
    with pytest.raises(ValueError):
        to_bool("maybe")
