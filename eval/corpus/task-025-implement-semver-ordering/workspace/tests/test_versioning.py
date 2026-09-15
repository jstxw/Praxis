import pytest

from versioning import compare_versions, parse_version, sort_versions


# ── parse_version ───────────────────────────────────────────────────


def test_parse_plain():
    assert parse_version("1.2.3") == (1, 2, 3, (), ())


def test_parse_full():
    assert parse_version("1.2.3-rc.1+exp.sha.5114f85") == (
        1, 2, 3, ("rc", 1), ("exp", "sha", "5114f85"),
    )


def test_parse_prerelease_types():
    major, minor, patch, pre, build = parse_version("0.0.0-alpha.10.x-y.0")
    assert pre == ("alpha", 10, "x-y", 0)
    assert type(pre[1]) is int and type(pre[3]) is int
    assert build == ()


def test_parse_build_leading_zeros_allowed_and_kept_as_str():
    assert parse_version("1.0.0+001.0a")[4] == ("001", "0a")


def test_parse_large_numbers():
    assert parse_version("10.200.3000")[:3] == (10, 200, 3000)


@pytest.mark.parametrize(
    "text",
    [
        "", "1", "1.2", "1.2.3.4", "01.2.3", "1.02.3", "1.2.03", "v1.2.3",
        " 1.2.3", "1.2.3 ", "1.2.3-", "1.2.3+", "1.2.3-alpha..1", "1.2.3-01",
        "1.2.3-alpha.01", "1.2.3-al_pha", "1.2.3+b..c", "-1.2.3", "1.2.x",
        "1.2.3-αβ", "１.2.3",
    ],
)
def test_parse_invalid(text):
    with pytest.raises(ValueError):
        parse_version(text)


# ── compare_versions ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "a, b",
    [
        ("1.0.0", "2.0.0"),
        ("2.0.0", "2.1.0"),
        ("2.1.0", "2.1.1"),
        ("1.9.0", "1.10.0"),
        ("1.0.0-alpha", "1.0.0"),
        ("1.0.0-alpha", "1.0.0-alpha.1"),
        ("1.0.0-alpha.1", "1.0.0-alpha.beta"),
        ("1.0.0-alpha.beta", "1.0.0-beta"),
        ("1.0.0-beta", "1.0.0-beta.2"),
        ("1.0.0-beta.2", "1.0.0-beta.11"),
        ("1.0.0-beta.11", "1.0.0-rc.1"),
        ("1.0.0-rc.1", "1.0.0"),
        ("1.0.0-Beta", "1.0.0-alpha"),
        ("1.0.0-9", "1.0.0-a"),
        ("1.0.0-999", "1.0.0-1a"),
        ("1.0.0-rc.9", "1.0.0-rc.10"),
        ("0.9.9", "1.0.0-alpha"),
    ],
)
def test_compare_less(a, b):
    assert compare_versions(a, b) == -1
    assert compare_versions(b, a) == 1


def test_compare_equal():
    assert compare_versions("1.2.3", "1.2.3") == 0


def test_compare_ignores_build_metadata():
    assert compare_versions("1.0.0+build.1", "1.0.0+build.2") == 0
    assert compare_versions("1.0.0-rc.1+a", "1.0.0-rc.1") == 0


def test_compare_numeric_identifier_not_string_compare():
    assert compare_versions("1.0.0-2", "1.0.0-10") == -1


def test_compare_invalid_raises():
    with pytest.raises(ValueError):
        compare_versions("1.0.0", "1.0")


# ── sort_versions ───────────────────────────────────────────────────


def test_sort_semver_spec_example():
    expected = [
        "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
        "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0",
    ]
    shuffled = [expected[i] for i in (5, 0, 7, 3, 1, 6, 2, 4)]
    assert sort_versions(shuffled) == expected


def test_sort_is_stable_for_equal_precedence():
    versions = ["1.0.0+zzz", "0.1.0", "1.0.0+aaa", "1.0.0", "1.0.0+mmm"]
    assert sort_versions(versions) == ["0.1.0", "1.0.0+zzz", "1.0.0+aaa", "1.0.0", "1.0.0+mmm"]


def test_sort_does_not_mutate_input():
    versions = ["2.0.0", "1.0.0"]
    result = sort_versions(versions)
    assert versions == ["2.0.0", "1.0.0"]
    assert result == ["1.0.0", "2.0.0"]
    assert result is not versions


def test_sort_empty():
    assert sort_versions([]) == []


def test_sort_invalid_raises():
    with pytest.raises(ValueError):
        sort_versions(["1.0.0", "nope"])
