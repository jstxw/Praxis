"""Deterministic key hashing (unrelated to the cache)."""

import hashlib


def stable_key(*parts: str) -> str:
    joined = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:16]
