"""Shipping cost rules. All amounts are integer cents."""

FREE_SHIPPING_THRESHOLD_CENTS = 5000
FLAT_RATE_CENTS = 499
EXPRESS_SURCHARGE_CENTS = 1500


def qualifies_for_free_shipping(subtotal_cents: int) -> bool:
    return subtotal_cents >= FREE_SHIPPING_THRESHOLD_CENTS


def shipping_cost(subtotal_cents: int) -> int:
    if qualifies_for_free_shipping(subtotal_cents):
        return 0
    return FLAT_RATE_CENTS


def express_cost(subtotal_cents: int) -> int:
    return shipping_cost(subtotal_cents) + EXPRESS_SURCHARGE_CENTS
