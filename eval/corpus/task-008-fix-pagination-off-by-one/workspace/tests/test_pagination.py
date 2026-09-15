import pytest

from pagination import get_page, page_count

ITEMS = list(range(1, 11))  # 10 items: 1..10


def test_page_count_partial_last_page():
    assert page_count(10, 3) == 4


def test_page_count_exact_multiple():
    assert page_count(10, 5) == 2


def test_page_count_zero_items():
    assert page_count(0, 5) == 0


def test_page_count_single_item():
    assert page_count(1, 5) == 1


def test_page_count_rejects_bad_per_page():
    with pytest.raises(ValueError):
        page_count(10, 0)


def test_first_page():
    assert get_page(ITEMS, 1, 3) == [1, 2, 3]


def test_middle_page():
    assert get_page(ITEMS, 2, 3) == [4, 5, 6]


def test_last_partial_page():
    assert get_page(ITEMS, 4, 3) == [10]


def test_page_past_end_is_empty():
    assert get_page(ITEMS, 5, 3) == []


def test_page_zero_is_empty():
    assert get_page(ITEMS, 0, 3) == []


def test_negative_page_is_empty():
    assert get_page(ITEMS, -1, 3) == []


def test_get_page_rejects_bad_per_page():
    with pytest.raises(ValueError):
        get_page(ITEMS, 1, 0)


def test_all_pages_cover_items_exactly_once():
    per_page = 4
    collected = []
    for p in range(1, page_count(len(ITEMS), per_page) + 1):
        collected.extend(get_page(ITEMS, p, per_page))
    assert collected == ITEMS
