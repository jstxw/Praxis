from flatten_json import flatten_dict


def test_flatten_nested():
    assert flatten_dict({"a": {"b": 1, "c": {"d": 2}}, "e": 3}) == {"a.b": 1, "a.c.d": 2, "e": 3}


def test_flatten_empty_nested_kept():
    assert flatten_dict({"a": {}}) == {"a": {}}
