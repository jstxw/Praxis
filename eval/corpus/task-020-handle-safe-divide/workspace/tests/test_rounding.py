from rounding import round_half_up


def test_round_half_up():
    assert round_half_up("2.675") == "2.68"
    assert round_half_up("2.665") == "2.67"
    assert round_half_up("-1.005") == "-1.01"
    assert round_half_up("7", places=0) == "7"
