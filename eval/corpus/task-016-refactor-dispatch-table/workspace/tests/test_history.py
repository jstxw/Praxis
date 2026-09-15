from history import History


def test_history_bounded():
    h = History(limit=2)
    for e in ["a", "b", "c"]:
        h.record(e)
    assert h.last(5) == ["b", "c"]


def test_history_last_zero():
    h = History(limit=3)
    h.record("a")
    assert h.last(0) == []
