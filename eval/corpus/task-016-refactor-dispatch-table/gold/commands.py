"""A tiny command interpreter for binary arithmetic operations."""

import operator

OPERATIONS = {
    "add": operator.add,
    "sub": operator.sub,
    "mul": operator.mul,
    "div": operator.truediv,
    "pow": operator.pow,
    "max": max,
    "min": min,
}


def run(command: str, a, b):
    operation = OPERATIONS.get(command.strip().lower())
    if operation is None:
        raise ValueError(f"unknown command: {command!r}")
    return operation(a, b)
