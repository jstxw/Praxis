"""Number formatting helpers (unrelated to calculator)."""


def with_commas(n: int) -> str:
    return f"{n:,}"


def percent(part: float, whole: float, digits: int = 1) -> str:
    if whole == 0:
        return "n/a"
    return f"{100 * part / whole:.{digits}f}%"
