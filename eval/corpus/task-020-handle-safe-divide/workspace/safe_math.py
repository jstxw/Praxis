"""Arithmetic helpers that fail soft."""


def safe_int(text: str, default: int = 0) -> int:
    try:
        return int(text)
    except ValueError:
        return default


def safe_divide(a, b, default=None):
    """Return a / b, or `default` if b is zero."""
    return a / b
