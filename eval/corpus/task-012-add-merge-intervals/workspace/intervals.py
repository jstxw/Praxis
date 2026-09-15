"""Closed-interval helpers. An interval is a (start, end) pair with start <= end."""


def overlaps(a, b) -> bool:
    """True if closed intervals a and b share at least one point."""
    return a[0] <= b[1] and b[0] <= a[1]
