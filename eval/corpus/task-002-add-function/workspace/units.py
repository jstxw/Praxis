"""Unit conversion helpers (unrelated to stats)."""


def c_to_f(celsius: float) -> float:
    return celsius * 9 / 5 + 32


def km_to_miles(km: float) -> float:
    return km * 0.621371
