from sku import is_valid_sku, normalize_sku


def test_is_valid_sku():
    assert is_valid_sku("AB123")
    assert not is_valid_sku("ab123")
    assert not is_valid_sku("ABCD1")


def test_normalize_sku():
    assert normalize_sku(" ab-123 ") == "AB123"
