"""Package weight helpers (unrelated to shipping rules)."""


def grams_to_ounces(grams: float) -> float:
    return grams / 28.349523125


def total_grams(items: list[tuple[int, int]]) -> int:
    """items are (unit_grams, quantity) pairs."""
    return sum(unit * qty for unit, qty in items)
