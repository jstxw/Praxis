import math

import pytest

from angles import deg_to_rad, normalize_degrees


def test_deg_to_rad():
    assert deg_to_rad(180) == pytest.approx(math.pi)


def test_normalize_degrees():
    assert normalize_degrees(370) == 10
    assert normalize_degrees(-90) == 270
    assert normalize_degrees(360) == 0
