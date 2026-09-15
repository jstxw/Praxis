"""Roman numeral conversion.

Symbols: I=1, V=5, X=10, L=50, C=100, D=500, M=1000.
Canonical form uses the subtractive pairs IV=4, IX=9, XL=40, XC=90, CD=400,
CM=900, largest values first, with no symbol repeated more than three times.
Supported range is 1..3999 inclusive.
"""


def to_roman(n: int) -> str:
    raise NotImplementedError


def from_roman(s: str) -> int:
    raise NotImplementedError
