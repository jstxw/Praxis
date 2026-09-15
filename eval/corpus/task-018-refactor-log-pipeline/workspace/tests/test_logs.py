import ast
import inspect
import itertools

import pytest

import logs

LINES = [
    "INFO [db] connected",
    "DEBUG [db] query plan",
    "  WARN [http] slow response  ",
    "garbage line",
    "ERROR [db] timeout",
    "INFO [http] GET /",
    "info [http] lowercase level is malformed",
    "INFO [] empty component",
    "INFO http missing brackets",
    "INFO [auth]",
    "",
]


# ── behavior that must be preserved ─────────────────────────────────


def test_summarize_default_level():
    assert logs.summarize(LINES) == {
        "counts": {"db": 2, "http": 2, "auth": 1},
        "skipped": 5,
        "total": 5,
    }


def test_summarize_counts_keys_first_seen_order():
    assert list(logs.summarize(LINES)["counts"]) == ["db", "http", "auth"]


def test_summarize_warn_level():
    assert logs.summarize(LINES, "WARN") == {
        "counts": {"http": 1, "db": 1},
        "skipped": 5,
        "total": 2,
    }


def test_summarize_debug_level_order():
    assert list(logs.summarize(LINES, "DEBUG")["counts"]) == ["db", "http", "auth"]
    assert logs.summarize(LINES, "DEBUG")["total"] == 6


def test_summarize_accepts_generator():
    assert logs.summarize(iter(LINES))["total"] == 5


def test_summarize_unknown_level():
    with pytest.raises(ValueError):
        logs.summarize(LINES, "FATAL")


# ── the refactor ────────────────────────────────────────────────────


def test_parse_line_well_formed():
    assert logs.parse_line("  WARN [http] slow response  ") == {
        "level": "WARN",
        "component": "http",
        "message": "slow response",
    }


def test_parse_line_empty_message():
    assert logs.parse_line("INFO [auth]") == {"level": "INFO", "component": "auth", "message": ""}


@pytest.mark.parametrize(
    "line", ["garbage line", "info [x] y", "INFO [] y", "INFO x y", "", "INFO"]
)
def test_parse_line_malformed(line):
    assert logs.parse_line(line) is None


def test_filter_level():
    records = [logs.parse_line(line) for line in LINES]
    records = [r for r in records if r]
    kept = list(logs.filter_level(records, "WARN"))
    assert [r["component"] for r in kept] == ["http", "db"]


def test_filter_level_is_lazy_on_infinite_input():
    def endless():
        for i in itertools.count():
            yield {"level": "DEBUG" if i % 3 else "ERROR", "component": f"c{i}", "message": ""}

    first_two = list(itertools.islice(logs.filter_level(endless(), "ERROR"), 2))
    assert [r["component"] for r in first_two] == ["c0", "c3"]


def test_filter_level_unknown_level():
    with pytest.raises(ValueError):
        list(logs.filter_level([], "LOUD"))


def test_count_by_component_order():
    records = [{"component": c, "level": "INFO", "message": ""} for c in "babca"]
    result = logs.count_by_component(records)
    assert result == {"b": 2, "a": 2, "c": 1}
    assert list(result) == ["b", "a", "c"]


def test_summarize_has_no_loops():
    tree = ast.parse(inspect.getsource(logs.summarize))
    loops = [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While, ast.AsyncFor))]
    assert not loops, "summarize() should compose parse_line/filter_level/count_by_component"
