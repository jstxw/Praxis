"""Iterative Fibonacci (unrelated to factorial)."""


def fib(n: int) -> int:
    if n < 0:
        raise ValueError("fib undefined for negatives")
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a
