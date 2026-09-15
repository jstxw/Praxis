from backoff_table import schedule


def test_schedule_uncapped():
    assert schedule(1, 2, 4) == [1, 2, 4, 8]


def test_schedule_capped():
    assert schedule(1, 3, 4, cap=5) == [1, 3, 5, 5]


def test_schedule_zero_steps():
    assert schedule(1, 2, 0) == []
