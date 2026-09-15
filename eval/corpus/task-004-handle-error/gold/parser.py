"""Parse a comma-separated list of ages."""


def parse_ages(text: str) -> list[int]:
    """Return a list of integer ages parsed from a comma-separated string.

    Empty/whitespace input yields []. Tokens that are not valid integers
    (including decimals such as '1.5') are skipped.
    """
    ages: list[int] = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            ages.append(int(token))
        except ValueError:
            continue
    return ages
