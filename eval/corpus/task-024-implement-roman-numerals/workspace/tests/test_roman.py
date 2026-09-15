import pytest

from roman import from_roman, to_roman


@pytest.mark.parametrize(
    "n, s",
    [
        (1, "I"),
        (4, "IV"),
        (9, "IX"),
        (14, "XIV"),
        (40, "XL"),
        (90, "XC"),
        (400, "CD"),
        (944, "CMXLIV"),
        (1994, "MCMXCIV"),
        (2024, "MMXXIV"),
        (3999, "MMMCMXCIX"),
    ],
)
def test_known_values(n, s):
    assert to_roman(n) == s
    assert from_roman(s) == n


def test_round_trip_full_range():
    for n in range(1, 4000):
        assert from_roman(to_roman(n)) == n


@pytest.mark.parametrize("n", [0, -1, 4000, 10**6])
def test_to_roman_out_of_range(n):
    with pytest.raises(ValueError):
        to_roman(n)


@pytest.mark.parametrize("n", [True, 3.0, "12", None])
def test_to_roman_type_errors(n):
    with pytest.raises(TypeError):
        to_roman(n)


@pytest.mark.parametrize(
    "s",
    ["", "iv", "Xiv", " X", "X ", "ABC", "IIII", "VV", "LL", "DD", "MMMM",
     "IM", "IC", "XM", "VX", "IL", "IXI", "XCX", "CMCM", "IVI"],
)
def test_from_roman_rejects_invalid(s):
    with pytest.raises(ValueError):
        from_roman(s)
