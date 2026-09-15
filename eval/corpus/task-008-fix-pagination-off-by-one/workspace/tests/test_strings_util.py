from strings_util import is_blank, truncate


def test_truncate_short_text_unchanged():
    assert truncate("hello", 10) == "hello"


def test_truncate_long_text():
    assert truncate("hello world", 8) == "hello..."


def test_truncate_tiny_width():
    assert truncate("hello", 2) == ".."


def test_is_blank():
    assert is_blank("  \t")
    assert not is_blank(" x ")
