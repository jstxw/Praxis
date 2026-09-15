from tax import tax_cents


def test_tax_cents_exact():
    assert tax_cents(10000, 825) == 825


def test_tax_cents_rounds_half_up():
    assert tax_cents(150, 5000) == 75
    assert tax_cents(1, 5000) == 1
    assert tax_cents(1, 4999) == 0
