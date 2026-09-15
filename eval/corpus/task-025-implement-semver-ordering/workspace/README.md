# versioning

Implement the following in `versioning.py`. This is a subset of
[Semantic Versioning 2.0.0](https://semver.org).

## Format

```
MAJOR.MINOR.PATCH[-PRERELEASE][+BUILD]
```

- `MAJOR`, `MINOR`, `PATCH` are non-negative integers written in decimal
  digits with **no leading zeros** (`0` is fine, `01` is invalid).
- `PRERELEASE` (optional, after `-`) is one or more dot-separated
  identifiers. Each identifier is non-empty and uses only `[0-9A-Za-z-]`.
  An identifier made only of digits must not have leading zeros
  (`alpha.0` is fine, `alpha.01` is invalid).
- `BUILD` (optional, after `+`) is one or more dot-separated non-empty
  identifiers using only `[0-9A-Za-z-]` (leading zeros allowed).
- No surrounding whitespace, no `v` prefix. Anything else is invalid.

## `parse_version(text) -> tuple`

Returns `(major, minor, patch, prerelease, build)` where `major`, `minor`,
`patch` are `int`, `prerelease` is a `tuple` of identifiers (numeric-only
identifiers converted to `int`, others kept as `str`; empty tuple when
absent) and `build` is a `tuple` of `str` (empty tuple when absent).
Raises `ValueError` for invalid input.

Example: `parse_version("1.2.3-rc.1+exp.sha.5114f85")` ->
`(1, 2, 3, ("rc", 1), ("exp", "sha", "5114f85"))`

## `compare_versions(a, b) -> int`

Takes two version strings; returns `-1` if `a < b`, `0` if they have equal
precedence, `1` if `a > b`. Raises `ValueError` if either is invalid.
Precedence:

1. Compare `MAJOR`, `MINOR`, `PATCH` numerically, in that order.
2. If those are equal, a version **with** a pre-release is **lower** than
   the same version without one: `1.0.0-alpha < 1.0.0`.
3. Two pre-releases are compared identifier by identifier, left to right:
   - both numeric: compare as integers (`2 < 10`);
   - both alphanumeric: compare lexically in ASCII order (`"Beta" < "alpha"`);
   - numeric vs alphanumeric: numeric is lower;
   - if all shared identifiers are equal, the version with **fewer**
     identifiers is lower (`1.0.0-alpha < 1.0.0-alpha.1`).
4. Build metadata is ignored: `1.0.0+a` and `1.0.0+b` have equal precedence.

## `sort_versions(versions) -> list[str]`

Returns a new list of the input strings sorted ascending by precedence.
The sort is **stable**: strings of equal precedence keep their original
relative order. The input list is not modified. Raises `ValueError` if any
string is invalid.
