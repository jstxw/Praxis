"""A shopping cart. Line items are dicts: {"sku": str, "price_cents": int, "qty": int}."""


def _clone_lines(lines):
    return [dict(line) for line in lines]


class Cart:
    def __init__(self, lines=None):
        self.lines = _clone_lines(lines) if lines is not None else []

    def add(self, sku: str, price_cents: int, qty: int = 1) -> None:
        for line in self.lines:
            if line["sku"] == sku:
                line["qty"] += qty
                return
        self.lines.append({"sku": sku, "price_cents": price_cents, "qty": qty})

    def set_quantity(self, sku: str, qty: int) -> None:
        for line in self.lines:
            if line["sku"] == sku:
                line["qty"] = qty
                return
        raise KeyError(sku)

    def total_cents(self) -> int:
        return sum(line["price_cents"] * line["qty"] for line in self.lines)

    def copy(self) -> "Cart":
        return Cart(self.lines)


def merge(a: Cart, b: Cart) -> Cart:
    """Return a new cart containing the lines of both carts (quantities summed by sku)."""
    result = a.copy()
    for line in b.lines:
        result.add(line["sku"], line["price_cents"], line["qty"])
    return result
