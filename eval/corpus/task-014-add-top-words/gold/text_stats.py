"""Word statistics."""

import re

_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")


def tokenize(text: str) -> list[str]:
    """Split text into words (letters/digits, allowing inner apostrophes)."""
    return _WORD.findall(text)


def word_count(text: str) -> int:
    return len(tokenize(text))


def top_words(text: str, n: int) -> list[tuple[str, int]]:
    """Return the n most frequent casefolded words; ties keep first-occurrence order."""
    if n <= 0:
        return []
    counts: dict[str, int] = {}
    for word in tokenize(text):
        key = word.casefold()
        counts[key] = counts.get(key, 0) + 1
    # dicts preserve insertion (first-occurrence) order and sorted() is stable
    ranked = sorted(counts.items(), key=lambda item: -item[1])
    return ranked[:n]
