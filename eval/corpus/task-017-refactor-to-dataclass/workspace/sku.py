"""SKU helpers (unrelated to the Product model)."""

import re

_SKU = re.compile(r"^[A-Z]{1,3}\d{1,6}$")


def is_valid_sku(sku: str) -> bool:
    return bool(_SKU.match(sku))


def normalize_sku(sku: str) -> str:
    return sku.strip().upper().replace("-", "")
