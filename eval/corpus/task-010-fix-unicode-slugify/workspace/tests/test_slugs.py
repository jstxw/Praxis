from slugs import slugify


def test_simple_words():
    assert slugify("Hello World") == "hello-world"


def test_digits_kept():
    assert slugify("Top 10 Tips") == "top-10-tips"


def test_punctuation_runs_collapse():
    assert slugify("Hello,   World!!") == "hello-world"


def test_no_leading_or_trailing_hyphens():
    assert slugify("  --Hello--  ") == "hello"


def test_accents_are_stripped():
    assert slugify("Crème Brûlée") == "creme-brulee"


def test_uppercase_accents():
    assert slugify("ÉCOLE Ñandú") == "ecole-nandu"


def test_non_latin_script_dropped_like_punctuation():
    assert slugify("日本 guide 2024") == "guide-2024"


def test_only_non_ascii_returns_empty():
    assert slugify("日本語") == ""


def test_empty_input():
    assert slugify("") == ""


def test_all_punctuation():
    assert slugify("!!! ---") == ""


def test_underscore_is_separator():
    assert slugify("snake_case_name") == "snake-case-name"


def test_compatibility_characters_normalized():
    # U+FB01 LATIN SMALL LIGATURE FI and fullwidth digits decompose under NFKD
    assert slugify("ﬁle １２") == "file-12"


def test_max_length_truncates():
    assert slugify("hello world", max_length=5) == "hello"


def test_max_length_does_not_end_with_hyphen():
    assert slugify("hello world", max_length=6) == "hello"


def test_max_length_larger_than_slug():
    assert slugify("hi there", max_length=100) == "hi-there"
