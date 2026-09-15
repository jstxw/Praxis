"""Angle helpers (unrelated to the geometry package)."""

import math


def deg_to_rad(deg: float) -> float:
    return deg * math.pi / 180


def normalize_degrees(deg: float) -> float:
    """Map any angle into [0, 360)."""
    return deg % 360
