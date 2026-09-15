"""Bracket balance checker (self-contained; unrelated to stack.py)."""

PAIRS = {")": "(", "]": "[", "}": "{"}


def is_balanced(text: str) -> bool:
    seen: list[str] = []
    for ch in text:
        if ch in "([{":
            seen.append(ch)
        elif ch in PAIRS:
            if not seen or seen.pop() != PAIRS[ch]:
                return False
    return not seen
