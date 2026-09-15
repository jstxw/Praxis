import ast
import inspect

import records
from records import parse_records


def test_all_valid():
    text = '{"id": 1, "name": "a"}\n{"id": 2, "name": "b"}\n'
    assert parse_records(text) == ([{"id": 1, "name": "a"}, {"id": 2, "name": "b"}], [])


def test_empty_text():
    assert parse_records("") == ([], [])


def test_blank_lines_skipped_but_counted():
    text = '\n   \n{"id": 1}\n\n{broken\n'
    recs, errs = parse_records(text)
    assert recs == [{"id": 1}]
    assert [e[0] for e in errs] == [5]


def test_invalid_json_reason():
    recs, errs = parse_records('{"id": 1}\nnot json at all\n')
    assert recs == [{"id": 1}]
    assert len(errs) == 1
    line, reason = errs[0]
    assert line == 2
    assert reason.startswith("invalid json")


def test_not_an_object():
    recs, errs = parse_records('[1, 2]\n"just a string"\n42\n{"id": 7}')
    assert recs == [{"id": 7}]
    assert errs == [(1, "not an object"), (2, "not an object"), (3, "not an object")]


def test_null_is_not_an_object():
    assert parse_records("null") == ([], [(1, "not an object")])


def test_missing_id():
    recs, errs = parse_records('{"name": "x"}\n{"id": null}')
    assert recs == [{"id": None}]
    assert errs == [(1, "missing id")]


def test_errors_in_input_order_mixed():
    text = "\n".join(['{"id": 1}', "{", "[]", '{"x": 1}', '{"id": 2}'])
    recs, errs = parse_records(text)
    assert [r["id"] for r in recs] == [1, 2]
    assert [(n, r.split(":")[0]) for n, r in errs] == [
        (2, "invalid json"),
        (3, "not an object"),
        (4, "missing id"),
    ]


def test_unicode_preserved():
    text = '{"id": "ü-1", "name": "Zoë 😀"}\n{"id": 2, "city": "東京"}'
    recs, errs = parse_records(text)
    assert errs == []
    assert recs[0]["name"] == "Zoë 😀"
    assert recs[1]["city"] == "東京"


def test_crlf_line_endings():
    recs, errs = parse_records('{"id": 1}\r\n{"id": 2}\r\n')
    assert [r["id"] for r in recs] == [1, 2]
    assert errs == []


def test_returns_tuple_of_lists_and_tuples():
    result = parse_records('{"id": 1}\nbad')
    assert isinstance(result, tuple) and len(result) == 2
    assert all(type(e) is tuple for e in result[1])


def test_no_broad_exception_handlers():
    tree = ast.parse(inspect.getsource(records))
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            assert node.type is not None, "bare except is not allowed"
            names = {n.id for n in ast.walk(node.type) if isinstance(n, ast.Name)}
            names |= {n.attr for n in ast.walk(node.type) if isinstance(n, ast.Attribute)}
            assert not names & {"Exception", "BaseException"}, "catch only JSON decode errors"
