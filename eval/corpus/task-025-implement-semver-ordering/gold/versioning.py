"""Semantic version parsing and ordering. See README.md for the spec."""

import functools
import re

_NUM = r"0|[1-9][0-9]*"
_IDENT = r"[0-9A-Za-z-]+"
_VERSION = re.compile(
    rf"({_NUM})\.({_NUM})\.({_NUM})"
    rf"(?:-({_IDENT}(?:\.{_IDENT})*))?"
    rf"(?:\+({_IDENT}(?:\.{_IDENT})*))?"
)


def parse_version(text: str) -> tuple:
    if not isinstance(text, str):
        raise ValueError(f"invalid version: {text!r}")
    match = _VERSION.fullmatch(text)
    if match is None:
        raise ValueError(f"invalid version: {text!r}")
    major, minor, patch, pre, build = match.groups()
    prerelease: list = []
    if pre is not None:
        for ident in pre.split("."):
            if ident.isdigit():
                if len(ident) > 1 and ident[0] == "0":
                    raise ValueError(f"leading zero in pre-release: {text!r}")
                prerelease.append(int(ident))
            else:
                prerelease.append(ident)
    return (
        int(major),
        int(minor),
        int(patch),
        tuple(prerelease),
        tuple(build.split(".")) if build is not None else (),
    )


def _cmp(x, y) -> int:
    return (x > y) - (x < y)


def _compare_identifier(x, y) -> int:
    x_num = isinstance(x, int)
    y_num = isinstance(y, int)
    if x_num and y_num:
        return _cmp(x, y)
    if x_num != y_num:
        return -1 if x_num else 1
    return _cmp(x, y)


def _compare_parsed(pa: tuple, pb: tuple) -> int:
    core = _cmp(pa[:3], pb[:3])
    if core:
        return core
    pre_a, pre_b = pa[3], pb[3]
    if not pre_a or not pre_b:
        # no pre-release ranks higher than having one
        return _cmp(not pre_a, not pre_b)
    for x, y in zip(pre_a, pre_b):
        result = _compare_identifier(x, y)
        if result:
            return result
    return _cmp(len(pre_a), len(pre_b))


def compare_versions(a: str, b: str) -> int:
    return _compare_parsed(parse_version(a), parse_version(b))


def sort_versions(versions: list[str]) -> list[str]:
    parsed = [(parse_version(v), v) for v in versions]
    parsed.sort(key=functools.cmp_to_key(lambda p, q: _compare_parsed(p[0], q[0])))
    return [v for _, v in parsed]
