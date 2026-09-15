from brackets import is_balanced


def test_balanced():
    assert is_balanced("(a[b]{c})")
    assert is_balanced("")


def test_unbalanced():
    assert not is_balanced("(]")
    assert not is_balanced("((")
    assert not is_balanced(")")
