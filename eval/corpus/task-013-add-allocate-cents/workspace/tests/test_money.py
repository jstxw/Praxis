import pytest

import money


def test_format_cents_unchanged():
    assert money.format_cents(123456) == "$1,234.56"
    assert money.format_cents(-5) == "-$0.05"


def test_allocate_even_split():
    assert money.allocate(100, [1, 1, 1, 1]) == [25, 25, 25, 25]


def test_allocate_thirds_tie_goes_to_lowest_index():
    assert money.allocate(100, [1, 1, 1]) == [34, 33, 33]


def test_allocate_two_leftover_cents():
    assert money.allocate(101, [1, 1, 1]) == [34, 34, 33]


def test_allocate_largest_remainder_not_first_index():
    # exact shares: 10*1/6=1.67, 10*2/6=3.33, 10*3/6=5.0 -> floors 1,3,5 leftover 1 -> index 0
    assert money.allocate(10, [1, 2, 3]) == [2, 3, 5]


def test_allocate_remainder_ordering_picks_largest_fraction():
    # exact shares 4.29, 4.29, 1.43 -> floors 4,4,1 leftover 1 -> index 2 (largest fraction .43)
    assert money.allocate(10, [3, 3, 1]) == [4, 4, 2]


def test_allocate_sum_is_exact():
    for total in range(0, 250):
        result = money.allocate(total, [7, 11, 13, 1])
        assert sum(result) == total


def test_allocate_zero_total():
    assert money.allocate(0, [1, 2]) == [0, 0]


def test_allocate_zero_weight_gets_nothing():
    assert money.allocate(5, [0, 1, 0, 1]) == [0, 3, 0, 2]


def test_allocate_one_cent_many_buckets():
    assert money.allocate(1, [1, 1, 1]) == [1, 0, 0]


def test_allocate_returns_ints():
    assert all(type(x) is int for x in money.allocate(10, [1, 2]))


def test_allocate_huge_amount_exact():
    total = 10**18 + 1
    assert money.allocate(total, [1, 1]) == [500000000000000001, 500000000000000000]


def test_allocate_huge_uneven_exact():
    total = 2**60 + 7
    result = money.allocate(total, [1, 2])
    assert result == [384307168202282328, 768614336404564655]
    assert sum(result) == total


def test_allocate_does_not_mutate_weights():
    weights = [3, 1]
    money.allocate(10, weights)
    assert weights == [3, 1]


@pytest.mark.parametrize(
    "total, weights",
    [(10, []), (10, [1, -1]), (10, [0, 0]), (-1, [1, 1])],
)
def test_allocate_invalid_input(total, weights):
    with pytest.raises(ValueError):
        money.allocate(total, weights)
