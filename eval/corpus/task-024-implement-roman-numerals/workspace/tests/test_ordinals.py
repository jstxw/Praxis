from ordinals import ordinal


def test_ordinal():
    assert [ordinal(i) for i in (1, 2, 3, 4, 11, 12, 13, 21, 102, 111)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "102nd", "111th",
    ]
