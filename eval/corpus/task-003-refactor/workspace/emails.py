"""Email helpers (unrelated to user-record processing)."""


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


def mask(email: str) -> str:
    local, _, domain = email.partition("@")
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}{'*' * (len(local) - 1)}@{domain}"
