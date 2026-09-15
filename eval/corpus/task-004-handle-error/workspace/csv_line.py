"""Tiny helper for joining fields into a CSV-ish line (unrelated to parse_ages)."""


def join_fields(fields: list[str], sep: str = ",") -> str:
    return sep.join(f.strip() for f in fields)


def count_fields(line: str, sep: str = ",") -> int:
    return 0 if line == "" else line.count(sep) + 1
