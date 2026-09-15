"""Headline casing (unrelated to slugs)."""

SMALL_WORDS = {"a", "an", "and", "of", "the", "to", "in"}


def headline(text: str) -> str:
    words = text.split()
    out = []
    for i, word in enumerate(words):
        lower = word.lower()
        if 0 < i < len(words) - 1 and lower in SMALL_WORDS:
            out.append(lower)
        else:
            out.append(lower[:1].upper() + lower[1:])
    return " ".join(out)
