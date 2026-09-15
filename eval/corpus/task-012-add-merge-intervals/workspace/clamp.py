"""Numeric clamp helpers (unrelated to intervals)."""


def clamp(value, low, high):
    if low > high:
        raise ValueError("low must be <= high")
    return max(low, min(high, value))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t
