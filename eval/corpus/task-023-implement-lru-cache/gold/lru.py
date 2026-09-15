"""A least-recently-used cache."""

from collections import OrderedDict


class LRUCache:
    """Fixed-capacity mapping that evicts the least-recently used key.

    - LRUCache(capacity): capacity < 1 -> ValueError.
    - put(key, value): insert/update; key becomes most-recently used; if the
      cache would exceed capacity, evict the least-recently used key.
    - get(key, default=None): value (key becomes most-recently used) or default
      (recency unchanged).
    - `key in cache`: membership test that does NOT change recency.
    - len(cache): number of entries.
    - keys(): list of keys, least- to most-recently used.
    - evictions: number of evictions so far.
    """

    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self.evictions = 0
        self._data: OrderedDict = OrderedDict()

    def put(self, key, value) -> None:
        if key in self._data:
            self._data.move_to_end(key)
            self._data[key] = value
            return
        self._data[key] = value
        if len(self._data) > self.capacity:
            self._data.popitem(last=False)
            self.evictions += 1

    def get(self, key, default=None):
        if key not in self._data:
            return default
        self._data.move_to_end(key)
        return self._data[key]

    def __contains__(self, key) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def keys(self) -> list:
        return list(self._data)
