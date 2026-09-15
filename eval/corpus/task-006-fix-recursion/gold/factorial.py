"""Compute n! recursively."""


def factorial(n: int) -> int:
    if n < 0:
        raise ValueError("factorial undefined for negatives")
    if n <= 1:
        return 1
    return n * factorial(n - 1)
