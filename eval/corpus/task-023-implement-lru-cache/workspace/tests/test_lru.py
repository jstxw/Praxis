import pytest

from lru import LRUCache


def test_put_and_get():
    c = LRUCache(2)
    c.put("a", 1)
    assert c.get("a") == 1


def test_missing_returns_default():
    c = LRUCache(2)
    assert c.get("zzz") is None
    assert c.get("zzz", default=42) == 42


@pytest.mark.parametrize("capacity", [0, -3])
def test_invalid_capacity(capacity):
    with pytest.raises(ValueError):
        LRUCache(capacity)


def test_evicts_least_recently_put():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("c", 3)
    assert "a" not in c
    assert c.keys() == ["b", "c"]
    assert c.evictions == 1


def test_get_refreshes_recency():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    c.get("a")
    c.put("c", 3)
    assert c.keys() == ["a", "c"]


def test_get_missing_does_not_change_order():
    c = LRUCache(3)
    c.put("a", 1)
    c.put("b", 2)
    c.get("missing")
    assert c.keys() == ["a", "b"]


def test_contains_does_not_refresh_recency():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    assert "a" in c
    c.put("c", 3)
    assert "a" not in c


def test_update_existing_key_refreshes_and_does_not_evict():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("a", 10)
    assert len(c) == 2
    assert c.evictions == 0
    assert c.keys() == ["b", "a"]
    c.put("c", 3)
    assert c.keys() == ["a", "c"]
    assert c.get("a") == 10


def test_capacity_one():
    c = LRUCache(1)
    c.put("a", 1)
    c.put("b", 2)
    assert c.keys() == ["b"]
    assert c.get("a", "gone") == "gone"
    assert c.evictions == 1


def test_stored_none_distinguishable_from_missing():
    c = LRUCache(2)
    c.put("k", None)
    sentinel = object()
    assert c.get("k", sentinel) is None
    assert c.get("other", sentinel) is sentinel


def test_falsy_keys():
    c = LRUCache(3)
    c.put(0, "zero")
    c.put("", "empty")
    assert c.get(0) == "zero" and c.get("") == "empty"
    assert 0 in c


def test_len_and_eviction_count_over_many_puts():
    c = LRUCache(3)
    for i in range(10):
        c.put(i, i)
    assert len(c) == 3
    assert c.keys() == [7, 8, 9]
    assert c.evictions == 7


def test_keys_returns_copy():
    c = LRUCache(2)
    c.put("a", 1)
    k = c.keys()
    k.append("x")
    assert c.keys() == ["a"]
