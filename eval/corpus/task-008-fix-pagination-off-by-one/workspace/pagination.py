"""1-indexed pagination helpers. There are off-by-one bugs in here."""


def page_count(total: int, per_page: int) -> int:
    """Return how many pages are needed to show `total` items."""
    if per_page < 1:
        raise ValueError("per_page must be >= 1")
    return total // per_page + 1


def get_page(items: list, page: int, per_page: int) -> list:
    """Return the items shown on the given 1-indexed page."""
    start = page * per_page
    end = start + per_page
    return items[start:end]
