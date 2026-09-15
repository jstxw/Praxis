"""Word statistics."""

import re

_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")


def tokenize(text: str) -> list[str]:
    """Split text into words (letters/digits, allowing inner apostrophes)."""
    return _WORD.findall(text)


def word_count(text: str) -> int:
    return len(tokenize(text))
