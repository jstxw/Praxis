from emails import domain_of, mask


def test_domain_of():
    assert domain_of("Alice@Example.COM") == "example.com"


def test_mask():
    assert mask("alice@x.com") == "a****@x.com"
    assert mask("a@x.com") == "*@x.com"
