import pytest

from durations import humanize_seconds


def test_humanize_seconds():
    assert humanize_seconds(0) == "0s"
    assert humanize_seconds(59) == "59s"
    assert humanize_seconds(3600) == "1h"
    assert humanize_seconds(3725) == "1h 2m 5s"


def test_humanize_negative():
    with pytest.raises(ValueError):
        humanize_seconds(-1)
