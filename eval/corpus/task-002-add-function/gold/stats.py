"""Statistics utilities."""


def mean(values: list[float]) -> float:
    """Return the arithmetic mean of values."""
    if not values:
        raise ValueError("mean of empty list")
    return sum(values) / len(values)


def median(values: list[float]) -> float:
    """Return the median of values without mutating the input."""
    if not values:
        raise ValueError("median of empty list")
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2
