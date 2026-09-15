"""Boolean/int coercion for config values (unrelated to parsing)."""

TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}


def to_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in TRUTHY:
        return True
    if v in FALSY:
        return False
    raise ValueError(f"not a boolean: {value!r}")
