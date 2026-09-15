"""Log summarization.

A well-formed line looks like:  "LEVEL [component] message text"
where LEVEL is one of DEBUG, INFO, WARN, ERROR (case-sensitive), component is a
non-empty run of characters without ']' and message may be empty. Surrounding
whitespace on the line is ignored. Anything else is malformed and skipped.
"""

LEVELS = ["DEBUG", "INFO", "WARN", "ERROR"]


def parse_line(line):
    """Parse one log line into a record dict, or return None if malformed."""
    stripped = line.strip()
    level, sep, rest = stripped.partition(" ")
    if level not in LEVELS or not sep or not rest.startswith("["):
        return None
    close = rest.find("]")
    if close <= 1:
        return None
    return {
        "level": level,
        "component": rest[1:close],
        "message": rest[close + 1 :].strip(),
    }


def _threshold(min_level):
    if min_level not in LEVELS:
        raise ValueError(f"unknown level: {min_level}")
    return LEVELS.index(min_level)


def filter_level(records, min_level):
    """Lazily yield records at or above min_level."""
    threshold = _threshold(min_level)

    def generate():
        for record in records:
            if LEVELS.index(record["level"]) >= threshold:
                yield record

    return generate()


def count_by_component(records):
    """Count records per component; keys in first-seen order."""
    counts = {}
    for record in records:
        counts[record["component"]] = counts.get(record["component"], 0) + 1
    return counts


def summarize(lines, min_level="INFO"):
    """Return {"counts": {component: n}, "skipped": n_malformed, "total": n_kept}.

    Counts keys appear in first-seen order.
    """
    _threshold(min_level)
    parsed = [parse_line(line) for line in lines]
    records = [record for record in parsed if record is not None]
    counts = count_by_component(filter_level(records, min_level))
    return {
        "counts": counts,
        "skipped": len(parsed) - len(records),
        "total": sum(counts.values()),
    }
