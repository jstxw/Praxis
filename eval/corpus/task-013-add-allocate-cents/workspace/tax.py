"""Tax helpers (unrelated to allocation)."""


def tax_cents(amount_cents: int, basis_points: int) -> int:
    """Tax on amount at a rate in basis points, rounded half-up to the cent."""
    return (amount_cents * basis_points + 5000) // 10000
