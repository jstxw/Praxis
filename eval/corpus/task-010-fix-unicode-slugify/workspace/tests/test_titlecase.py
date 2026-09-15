from titlecase import headline


def test_headline_small_words():
    assert headline("the lord of the rings") == "The Lord of the Rings"


def test_headline_last_word_capitalized():
    assert headline("what are you in") == "What Are You In"


def test_headline_empty():
    assert headline("") == ""
