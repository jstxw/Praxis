import text_stats


def test_word_count_unchanged():
    assert text_stats.word_count("it's a dog's life") == 4


def test_top_words_basic():
    assert text_stats.top_words("a b a c a b", 2) == [("a", 3), ("b", 2)]


def test_top_words_case_insensitive():
    assert text_stats.top_words("The cat THE dog the", 1) == [("the", 3)]


def test_top_words_casefold_not_lower():
    # "Straße".lower() == "straße" but casefold() == "strasse"
    assert text_stats.top_words("Straße STRASSE strasse", 1) == [("strasse", 3)]


def test_top_words_ties_keep_first_occurrence_order():
    text = "pear apple fig apple pear fig kiwi"
    assert text_stats.top_words(text, 3) == [("pear", 2), ("apple", 2), ("fig", 2)]


def test_top_words_ties_not_alphabetical():
    assert text_stats.top_words("zebra yak xylophone", 3) == [
        ("zebra", 1),
        ("yak", 1),
        ("xylophone", 1),
    ]


def test_top_words_tie_order_uses_first_not_last_occurrence():
    text = "b a a b c"
    assert text_stats.top_words(text, 2) == [("b", 2), ("a", 2)]


def test_top_words_n_larger_than_distinct():
    assert text_stats.top_words("x y x", 10) == [("x", 2), ("y", 1)]


def test_top_words_n_zero_or_negative():
    assert text_stats.top_words("x y x", 0) == []
    assert text_stats.top_words("x y x", -3) == []


def test_top_words_empty_text():
    assert text_stats.top_words("", 5) == []


def test_top_words_apostrophes_and_unicode():
    text = "Don't stop. don't STOP! Café café CAFÉ"
    assert text_stats.top_words(text, 3) == [("café", 3), ("don't", 2), ("stop", 2)]


def test_top_words_returns_tuples():
    result = text_stats.top_words("a a b", 2)
    assert all(type(item) is tuple for item in result)
