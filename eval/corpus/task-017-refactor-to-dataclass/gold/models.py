"""Domain models."""

import dataclasses


@dataclasses.dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price_cents: int
    tags: tuple[str, ...] = ()

    def __post_init__(self):
        if self.price_cents < 0:
            raise ValueError("price_cents must be >= 0")
        if not isinstance(self.tags, tuple):
            object.__setattr__(self, "tags", tuple(self.tags))

    def with_price(self, new_price_cents):
        return dataclasses.replace(self, price_cents=new_price_cents)

    def has_tag(self, tag):
        return tag in self.tags
