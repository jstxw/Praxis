"""Shipping cost rules. All amounts are integer cents."""


def qualifies_for_free_shipping(subtotal_cents: int) -> bool:
    return subtotal_cents >= 5000


def shipping_cost(subtotal_cents: int) -> int:
    if subtotal_cents >= 5000:
        return 0
    return 499


def express_cost(subtotal_cents: int) -> int:
    if subtotal_cents >= 5000:
        return 1500
    return 499 + 1500
