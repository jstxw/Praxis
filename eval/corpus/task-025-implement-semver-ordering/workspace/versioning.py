"""Semantic version parsing and ordering. See README.md for the spec."""


def parse_version(text: str) -> tuple:
    raise NotImplementedError


def compare_versions(a: str, b: str) -> int:
    raise NotImplementedError


def sort_versions(versions: list[str]) -> list[str]:
    raise NotImplementedError
