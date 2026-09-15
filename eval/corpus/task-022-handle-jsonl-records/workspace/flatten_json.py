"""Flatten nested dicts into dotted keys (unrelated to JSONL parsing)."""


def flatten_dict(data: dict, prefix: str = "") -> dict:
    out = {}
    for key, value in data.items():
        full = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            out.update(flatten_dict(value, full))
        else:
            out[full] = value
    return out
