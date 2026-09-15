"""Domain models."""


class Product:
    def __init__(self, sku, name, price_cents, tags=()):
        if price_cents < 0:
            raise ValueError("price_cents must be >= 0")
        self.sku = sku
        self.name = name
        self.price_cents = price_cents
        self.tags = tuple(tags)

    def __eq__(self, other):
        if other.__class__ is not self.__class__:
            return NotImplemented
        return (self.sku, self.name, self.price_cents, self.tags) == (
            other.sku,
            other.name,
            other.price_cents,
            other.tags,
        )

    def __hash__(self):
        return hash((self.sku, self.name, self.price_cents, self.tags))

    def __repr__(self):
        return (
            f"Product(sku={self.sku!r}, name={self.name!r}, "
            f"price_cents={self.price_cents!r}, tags={self.tags!r})"
        )

    def with_price(self, new_price_cents):
        return Product(self.sku, self.name, new_price_cents, self.tags)

    def has_tag(self, tag):
        return tag in self.tags
