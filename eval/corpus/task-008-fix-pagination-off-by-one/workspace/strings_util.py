"""Small string helpers (unrelated to pagination)."""


def truncate(text: str, width: int, suffix: str = "...") -> str:
    """Shorten text to at most `width` characters, appending suffix if cut."""
    if len(text) <= width:
        return text
    if width <= len(suffix):
        return suffix[:width]
    return text[: width - len(suffix)] + suffix


def is_blank(text: str) -> bool:
    return not text.strip()
