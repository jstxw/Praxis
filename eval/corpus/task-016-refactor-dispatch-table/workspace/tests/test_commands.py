import ast
import inspect

import pytest

import commands


@pytest.mark.parametrize(
    "cmd, a, b, expected",
    [
        ("add", 2, 3, 5),
        ("sub", 2, 3, -1),
        ("mul", 4, 3, 12),
        ("div", 7, 2, 3.5),
        ("pow", 2, 10, 1024),
        ("max", 2, 9, 9),
        ("min", 2, 9, 2),
    ],
)
def test_each_command(cmd, a, b, expected):
    assert commands.run(cmd, a, b) == expected


def test_case_and_whitespace_insensitive():
    assert commands.run("  ADD ", 1, 1) == 2


def test_unknown_command_message_contains_original():
    with pytest.raises(ValueError, match="Frobnicate"):
        commands.run("Frobnicate", 1, 2)


def test_div_by_zero_still_raises():
    with pytest.raises(ZeroDivisionError):
        commands.run("div", 1, 0)


def test_operations_table_exists():
    assert isinstance(commands.OPERATIONS, dict)
    assert set(commands.OPERATIONS) == {"add", "sub", "mul", "div", "pow", "max", "min"}
    assert all(callable(fn) for fn in commands.OPERATIONS.values())


def test_operations_entries_behave():
    assert commands.OPERATIONS["sub"](10, 4) == 6
    assert commands.OPERATIONS["max"](-1, -5) == -1


def test_run_uses_table_at_call_time(monkeypatch):
    monkeypatch.setitem(commands.OPERATIONS, "avg", lambda a, b: (a + b) / 2)
    assert commands.run("avg", 2, 4) == 3


def test_run_has_no_command_name_comparisons():
    tree = ast.parse(inspect.getsource(commands.run))
    names = {"add", "sub", "mul", "div", "pow", "max", "min"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            consts = {
                c.value
                for c in [node.left, *node.comparators]
                if isinstance(c, ast.Constant)
            }
            assert not consts & names, "run() still compares against command names"
        if isinstance(node, ast.Match):
            pytest.fail("run() must use OPERATIONS, not a match statement")
