"""A tiny command interpreter for binary arithmetic operations."""


def run(command: str, a, b):
    name = command.strip().lower()
    if name == "add":
        return a + b
    elif name == "sub":
        return a - b
    elif name == "mul":
        return a * b
    elif name == "div":
        return a / b
    elif name == "pow":
        return a**b
    elif name == "max":
        return max(a, b)
    elif name == "min":
        return min(a, b)
    else:
        raise ValueError(f"unknown command: {command!r}")
