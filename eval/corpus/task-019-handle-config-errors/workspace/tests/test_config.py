import pytest

from config import ConfigError, load_config


def test_basic_config():
    text = "host = example.com\nport=8080\n"
    assert load_config(text) == {"host": "example.com", "port": "8080"}


def test_comments_and_blank_lines_ignored():
    text = "# comment\n\n   # indented comment\nname = x\n\n"
    assert load_config(text) == {"name": "x"}


def test_required_present():
    assert load_config("a=1\nb=2", required=("a", "b")) == {"a": "1", "b": "2"}


def test_value_may_contain_equals():
    assert load_config("url = http://x/?a=1&b=2") == {"url": "http://x/?a=1&b=2"}


def test_empty_value_allowed():
    assert load_config("token =") == {"token": ""}


def test_config_error_is_value_error():
    assert issubclass(ConfigError, ValueError)


def test_missing_equals_reports_line_number():
    text = "a = 1\n\n# note\njust some words\n"
    with pytest.raises(ConfigError, match=r"line 4\b"):
        load_config(text)


def test_empty_key_reports_line_number():
    with pytest.raises(ConfigError, match=r"line 2\b"):
        load_config("a = 1\n = oops")


def test_duplicate_key_reports_key_and_second_line():
    text = "timeout = 1\nb = 2\ntimeout = 3\n"
    with pytest.raises(ConfigError) as info:
        load_config(text)
    message = str(info.value)
    assert "line 3" in message
    assert "timeout" in message


def test_duplicate_key_after_whitespace_normalization():
    with pytest.raises(ConfigError, match=r"line 2\b"):
        load_config("key=1\n  key  = 2")


def test_missing_required_lists_all_sorted():
    with pytest.raises(ConfigError) as info:
        load_config("b = 1", required=("zeta", "b", "alpha"))
    assert "alpha, zeta" in str(info.value)


def test_no_bare_builtin_exceptions_leak():
    for bad in ["novalue", "a=1\na=2", "=x"]:
        with pytest.raises(ConfigError):
            load_config(bad)
    with pytest.raises(ConfigError):
        load_config("", required=("x",))
