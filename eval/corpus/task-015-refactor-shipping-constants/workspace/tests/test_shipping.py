import ast
import inspect

import pytest

import shipping


@pytest.mark.parametrize("subtotal, expected", [(0, 499), (4999, 499), (5000, 0), (12000, 0)])
def test_shipping_cost(subtotal, expected):
    assert shipping.shipping_cost(subtotal) == expected


@pytest.mark.parametrize("subtotal, expected", [(0, 1999), (4999, 1999), (5000, 1500)])
def test_express_cost(subtotal, expected):
    assert shipping.express_cost(subtotal) == expected


def test_qualifies_boundary():
    assert not shipping.qualifies_for_free_shipping(4999)
    assert shipping.qualifies_for_free_shipping(5000)


def test_constants_defined():
    assert shipping.FREE_SHIPPING_THRESHOLD_CENTS == 5000
    assert shipping.FLAT_RATE_CENTS == 499
    assert shipping.EXPRESS_SURCHARGE_CENTS == 1500


@pytest.mark.parametrize(
    "fn_name", ["shipping_cost", "express_cost", "qualifies_for_free_shipping"]
)
def test_no_magic_numbers_in_function_bodies(fn_name):
    tree = ast.parse(inspect.getsource(getattr(shipping, fn_name)))
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, int)
    }
    assert not literals & {5000, 499, 1500}, f"{fn_name} still uses magic numbers: {literals}"


def test_behavior_follows_constants(monkeypatch):
    """Functions must read the constants at call time, not copies of the numbers."""
    monkeypatch.setattr(shipping, "FREE_SHIPPING_THRESHOLD_CENTS", 100)
    monkeypatch.setattr(shipping, "FLAT_RATE_CENTS", 7)
    monkeypatch.setattr(shipping, "EXPRESS_SURCHARGE_CENTS", 10)
    assert shipping.qualifies_for_free_shipping(100)
    assert shipping.shipping_cost(99) == 7
    assert shipping.express_cost(99) == 17
    assert shipping.express_cost(100) == 10
