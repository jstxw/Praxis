"""Bounded command history (unrelated to the dispatch logic)."""


class History:
    def __init__(self, limit: int):
        self.limit = limit
        self._entries: list[str] = []

    def record(self, entry: str) -> None:
        self._entries.append(entry)
        if len(self._entries) > self.limit:
            del self._entries[0]

    def last(self, n: int) -> list[str]:
        return self._entries[-n:] if n > 0 else []
