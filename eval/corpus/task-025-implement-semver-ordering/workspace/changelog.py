"""Changelog entry formatting (unrelated to version ordering)."""


def format_entry(version: str, changes: list[str]) -> str:
    lines = [f"## {version}", ""]
    if changes:
        lines.extend(f"- {c}" for c in changes)
    else:
        lines.append("- No changes.")
    return "\n".join(lines)
