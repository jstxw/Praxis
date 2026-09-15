"""Closed-interval helpers. An interval is a (start, end) pair with start <= end."""


def overlaps(a, b) -> bool:
    """True if closed intervals a and b share at least one point."""
    return a[0] <= b[1] and b[0] <= a[1]


def merge_intervals(intervals):
    """Merge overlapping or touching intervals; returns a new sorted list of tuples."""
    for start, end in intervals:
        if start > end:
            raise ValueError(f"invalid interval: ({start}, {end})")
    merged: list[tuple] = []
    for start, end in sorted((s, e) for s, e in intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
