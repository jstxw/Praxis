"""Iteration helpers."""


def flatten(nested):
    """Flatten one level of nesting: [[1, 2], [3]] -> [1, 2, 3]."""
    return [item for group in nested for item in group]


def chunked(iterable, size):
    """Split iterable into lists of up to `size` consecutive items."""
    if size < 1:
        raise ValueError("size must be >= 1")
    chunks = []
    current = []
    for item in iterable:
        current.append(item)
        if len(current) == size:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks
