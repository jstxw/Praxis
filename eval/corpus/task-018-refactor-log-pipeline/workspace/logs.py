"""Log summarization.

A well-formed line looks like:  "LEVEL [component] message text"
where LEVEL is one of DEBUG, INFO, WARN, ERROR (case-sensitive), component is a
non-empty run of characters without ']' and message may be empty. Surrounding
whitespace on the line is ignored. Anything else is malformed and skipped.
"""

LEVELS = ["DEBUG", "INFO", "WARN", "ERROR"]


def summarize(lines, min_level="INFO"):
    """Return {"counts": {component: n}, "skipped": n_malformed, "total": n_kept}.

    Counts keys appear in first-seen order.
    """
    if min_level not in LEVELS:
        raise ValueError(f"unknown level: {min_level}")
    threshold = LEVELS.index(min_level)
    counts = {}
    skipped = 0
    total = 0
    for raw in lines:
        line = raw.strip()
        level, sep, rest = line.partition(" ")
        if level not in LEVELS or not sep or not rest.startswith("["):
            skipped += 1
            continue
        close = rest.find("]")
        if close <= 1:
            skipped += 1
            continue
        component = rest[1:close]
        if LEVELS.index(level) < threshold:
            continue
        counts[component] = counts.get(component, 0) + 1
        total += 1
    return {"counts": counts, "skipped": skipped, "total": total}
