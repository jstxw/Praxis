"""Duration formatting (unrelated to log parsing)."""


def humanize_seconds(seconds: int) -> str:
    if seconds < 0:
        raise ValueError("seconds must be >= 0")
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)
