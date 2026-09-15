"""Parse simple `key = value` configuration text."""


class ConfigError(ValueError):
    """Raised for malformed or incomplete configuration."""


def load_config(text: str, required=()) -> dict[str, str]:
    config = {}
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        key, value = line.split("=")
        config[key.strip()] = value.strip()
    for key in required:
        config[key]
    return config
