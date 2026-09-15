"""Parse simple `key = value` configuration text."""


class ConfigError(ValueError):
    """Raised for malformed or incomplete configuration."""


def load_config(text: str, required=()) -> dict[str, str]:
    config: dict[str, str] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ConfigError(f"line {line_no}: expected 'key = value'")
        key = key.strip()
        if not key:
            raise ConfigError(f"line {line_no}: empty key")
        if key in config:
            raise ConfigError(f"line {line_no}: duplicate key {key!r}")
        config[key] = value.strip()
    missing = sorted(set(required) - set(config))
    if missing:
        raise ConfigError(f"missing required keys: {', '.join(missing)}")
    return config
