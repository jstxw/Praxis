"""A least-recently-used cache."""


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
        raise NotImplementedError
