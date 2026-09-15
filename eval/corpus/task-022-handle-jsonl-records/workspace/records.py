"""Parse JSON Lines records."""

import json


def parse_records(text: str):
    """Parse JSON Lines text. Returns (records, errors)."""
    records = [json.loads(line) for line in text.split("\n")]
    return records, []
