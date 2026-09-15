"""URL slug generation."""

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_length: int | None = None) -> str:
    """Turn arbitrary text into a URL slug.

    Rules:
    - NFKD-normalize and drop combining marks, so 'é' -> 'e'.
    - Lowercase.
    - Each run of characters that are not ASCII letters/digits becomes one '-'.
    - No leading or trailing '-'.
    - Characters with no ASCII equivalent are treated like punctuation.
    - If max_length is given, the result has at most max_length characters
      and does not end with '-'.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    slug = _NON_ALNUM.sub("-", stripped.lower()).strip("-")
    if max_length is not None:
        slug = slug[:max_length].rstrip("-")
    return slug
