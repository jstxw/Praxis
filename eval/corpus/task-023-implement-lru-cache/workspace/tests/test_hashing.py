from hashing import stable_key


def test_stable_key_deterministic():
    assert stable_key("a", "b") == stable_key("a", "b")
    assert len(stable_key("a")) == 16


def test_stable_key_separates_parts():
    assert stable_key("ab", "c") != stable_key("a", "bc")
