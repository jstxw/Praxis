"""Money helpers. Amounts are integer cents."""


def format_cents(cents: int) -> str:
    """Format integer cents as a dollar string, e.g. -1234 -> '-$12.34'."""
    sign = "-" if cents < 0 else ""
    dollars, rem = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{rem:02d}"


def allocate(total_cents: int, weights: list[int]) -> list[int]:
    """Split total_cents proportionally to weights (largest-remainder method)."""
    if total_cents < 0:
        raise ValueError("total_cents must be >= 0")
    if not weights:
        raise ValueError("weights must not be empty")
    if any(w < 0 for w in weights):
        raise ValueError("weights must be non-negative")
    weight_sum = sum(weights)
    if weight_sum == 0:
        raise ValueError("weights must not all be zero")
    shares = []
    remainders = []
    for index, weight in enumerate(weights):
        share, remainder = divmod(total_cents * weight, weight_sum)
        shares.append(share)
        remainders.append((-remainder, index))
    leftover = total_cents - sum(shares)
    for _, index in sorted(remainders)[:leftover]:
        shares[index] += 1
    return shares
