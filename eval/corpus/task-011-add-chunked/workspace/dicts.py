"""Dict helpers (unrelated to iterutils)."""


def invert(mapping: dict) -> dict:
    return {v: k for k, v in mapping.items()}


def pick(mapping: dict, keys) -> dict:
    return {k: mapping[k] for k in keys if k in mapping}
