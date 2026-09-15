import pytest

from cart import Cart, merge


def test_add_and_total():
    c = Cart()
    c.add("apple", 50, 3)
    c.add("pear", 120)
    assert c.total_cents() == 270


def test_add_same_sku_sums_quantity():
    c = Cart()
    c.add("apple", 50, 1)
    c.add("apple", 50, 2)
    assert c.lines == [{"sku": "apple", "price_cents": 50, "qty": 3}]


def test_set_quantity_unknown_sku_raises():
    with pytest.raises(KeyError):
        Cart().set_quantity("nope", 1)


def test_default_carts_are_independent():
    a = Cart()
    b = Cart()
    a.add("apple", 50)
    assert b.lines == []


def test_constructor_does_not_alias_caller_list():
    source = [{"sku": "apple", "price_cents": 50, "qty": 1}]
    c = Cart(source)
    c.add("pear", 120)
    assert source == [{"sku": "apple", "price_cents": 50, "qty": 1}]


def test_constructor_does_not_alias_caller_line_dicts():
    source = [{"sku": "apple", "price_cents": 50, "qty": 1}]
    c = Cart(source)
    c.set_quantity("apple", 9)
    assert source[0]["qty"] == 1


def test_copy_is_deep_for_line_items():
    original = Cart()
    original.add("apple", 50, 1)
    clone = original.copy()
    clone.set_quantity("apple", 5)
    assert original.total_cents() == 50
    assert clone.total_cents() == 250


def test_copy_new_lines_do_not_leak():
    original = Cart()
    clone = original.copy()
    clone.add("pear", 120)
    assert original.lines == []


def test_merge_returns_new_cart_without_mutating_inputs():
    a = Cart()
    a.add("apple", 50, 1)
    b = Cart()
    b.add("apple", 50, 2)
    b.add("pear", 120, 1)
    merged = merge(a, b)
    assert merged is not a and merged is not b
    assert merged.total_cents() == 270
    assert a.total_cents() == 50
    assert b.total_cents() == 220


def test_merge_result_independent_of_inputs():
    a = Cart()
    a.add("apple", 50, 1)
    merged = merge(a, Cart())
    merged.set_quantity("apple", 10)
    assert a.lines[0]["qty"] == 1


def test_line_order_is_preserved():
    c = Cart()
    for sku in ["c", "a", "b"]:
        c.add(sku, 1)
    assert [line["sku"] for line in c.copy().lines] == ["c", "a", "b"]
