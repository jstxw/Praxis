from formatting import percent, with_commas


def test_with_commas():
    assert with_commas(1234567) == "1,234,567"
    assert with_commas(12) == "12"


def test_percent():
    assert percent(1, 4) == "25.0%"
    assert percent(1, 3, digits=2) == "33.33%"


def test_percent_zero_whole():
    assert percent(1, 0) == "n/a"
