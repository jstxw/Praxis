from csv_line import count_fields, join_fields


def test_join_fields_strips():
    assert join_fields([" a", "b ", " c "]) == "a,b,c"


def test_count_fields():
    assert count_fields("") == 0
    assert count_fields("a") == 1
    assert count_fields("a,b,,c") == 4
