"""Parse JSON Lines records."""

import json


def parse_records(text: str):
    """Parse JSON Lines text. Returns (records, errors)."""
    records = []
    errors = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append((line_number, f"invalid json: {exc.msg}"))
            continue
        if not isinstance(value, dict):
            errors.append((line_number, "not an object"))
        elif "id" not in value:
            errors.append((line_number, "missing id"))
        else:
            records.append(value)
    return records, errors
