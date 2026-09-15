"""Money helpers. Amounts are integer cents."""


def format_cents(cents: int) -> str:
    """Format integer cents as a dollar string, e.g. -1234 -> '-$12.34'."""
    sign = "-" if cents < 0 else ""
    dollars, rem = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{rem:02d}"
