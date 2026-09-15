import dataclasses

import pytest

import models
from models import Product


def test_basic_attributes():
    p = Product("A1", "Mug", 1299, ("kitchen",))
    assert (p.sku, p.name, p.price_cents, p.tags) == ("A1", "Mug", 1299, ("kitchen",))


def test_default_tags():
    assert Product("A1", "Mug", 1299).tags == ()


def test_equality_and_hash():
    a = Product("A1", "Mug", 1299, ("x",))
    b = Product("A1", "Mug", 1299, ("x",))
    assert a == b
    assert len({a, b}) == 1
    assert a != Product("A1", "Mug", 1300, ("x",))


def test_repr():
    p = Product("A1", "Mug", 1299, ("x",))
    assert repr(p) == "Product(sku='A1', name='Mug', price_cents=1299, tags=('x',))"


def test_negative_price_rejected():
    with pytest.raises(ValueError):
        Product("A1", "Mug", -1)


def test_with_price_returns_new_instance():
    p = Product("A1", "Mug", 1299, ("x",))
    q = p.with_price(999)
    assert q.price_cents == 999 and q.tags == ("x",)
    assert p.price_cents == 1299
    assert q is not p


def test_with_price_validates():
    with pytest.raises(ValueError):
        Product("A1", "Mug", 1).with_price(-5)


def test_has_tag():
    p = Product("A1", "Mug", 1299, ("kitchen", "gift"))
    assert p.has_tag("gift") and not p.has_tag("garden")


def test_is_dataclass():
    assert dataclasses.is_dataclass(Product)


def test_is_frozen():
    p = Product("A1", "Mug", 1299)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.price_cents = 1


def test_field_order_and_defaults():
    fields = dataclasses.fields(Product)
    assert [f.name for f in fields] == ["sku", "name", "price_cents", "tags"]
    assert fields[3].default == ()


def test_list_tags_converted_to_tuple_and_hashable():
    p = Product("A1", "Mug", 1299, ["a", "b"])
    assert p.tags == ("a", "b")
    hash(p)


def test_list_tags_not_aliased():
    tags = ["a"]
    p = Product("A1", "Mug", 1299, tags)
    tags.append("b")
    assert p.tags == ("a",)


def test_dunders_are_generated_not_handwritten():
    for dunder in ("__init__", "__eq__", "__repr__"):
        fn = Product.__dict__[dunder]
        assert fn.__code__.co_filename != models.__file__, (
            f"{dunder} is still hand-written in models.py"
        )
