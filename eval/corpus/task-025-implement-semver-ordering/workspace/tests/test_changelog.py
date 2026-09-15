from changelog import format_entry


def test_format_entry():
    assert format_entry("1.0.0", ["Add x", "Fix y"]) == "## 1.0.0\n\n- Add x\n- Fix y"


def test_format_entry_no_changes():
    assert format_entry("1.0.1", []) == "## 1.0.1\n\n- No changes."
