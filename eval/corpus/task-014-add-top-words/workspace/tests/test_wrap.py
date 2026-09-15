from wrap import wrap


def test_wrap_basic():
    assert wrap("the quick brown fox", 10) == ["the quick", "brown fox"]


def test_wrap_long_word_kept_whole():
    assert wrap("supercalifragilistic is long", 5) == ["supercalifragilistic", "is", "long"]


def test_wrap_empty():
    assert wrap("", 10) == []
