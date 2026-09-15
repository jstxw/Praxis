from dicts import invert, pick


def test_invert():
    assert invert({"a": 1, "b": 2}) == {1: "a", 2: "b"}


def test_pick():
    assert pick({"a": 1, "b": 2, "c": 3}, ["a", "c", "z"]) == {"a": 1, "c": 3}
