"""Rounding helpers (unrelated to safe_divide)."""

from decimal import ROUND_HALF_UP, Decimal


def round_half_up(value: str, places: int = 2) -> str:
    quantum = Decimal(1).scaleb(-places)
    return str(Decimal(value).quantize(quantum, rounding=ROUND_HALF_UP))
