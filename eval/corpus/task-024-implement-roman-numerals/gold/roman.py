"""Roman numeral conversion.

Symbols: I=1, V=5, X=10, L=50, C=100, D=500, M=1000.
Canonical form uses the subtractive pairs IV=4, IX=9, XL=40, XC=90, CD=400,
CM=900, largest values first, with no symbol repeated more than three times.
Supported range is 1..3999 inclusive.
"""

_TABLE = [
    (1000, "M"),
    (900, "CM"),
    (500, "D"),
    (400, "CD"),
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
]
_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def to_roman(n: int) -> str:
    if isinstance(n, bool) or not isinstance(n, int):
        raise TypeError("to_roman expects an int")
    if not 1 <= n <= 3999:
        raise ValueError("n must be in 1..3999")
    parts = []
    for value, symbol in _TABLE:
        count, n = divmod(n, value)
        parts.append(symbol * count)
    return "".join(parts)


def from_roman(s: str) -> int:
    if not isinstance(s, str) or not s or any(ch not in _VALUES for ch in s):
        raise ValueError(f"invalid roman numeral: {s!r}")
    total = 0
    for i, ch in enumerate(s):
        value = _VALUES[ch]
        if i + 1 < len(s) and _VALUES[s[i + 1]] > value:
            total -= value
        else:
            total += value
    if not 1 <= total <= 3999 or to_roman(total) != s:
        raise ValueError(f"non-canonical roman numeral: {s!r}")
    return total
