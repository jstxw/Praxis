"""Discount helpers (unrelated to cart state)."""


def apply_percent_off(cents: int, percent: int) -> int:
    """Return price after a whole-percent discount, rounded down to the cent."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be in [0, 100]")
    return cents * (100 - percent) // 100
