"""A tiny 2D geometry library (see ../README.md)."""

import math

TOLERANCE = 1e-6


class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y

    def distance_to(self, other: "Point") -> float:
        return math.hypot(other.x - self.x, other.y - self.y)

    def __eq__(self, other):
        if not isinstance(other, Point):
            return NotImplemented
        return self.x == other.x and self.y == other.y

    def __hash__(self):
        return hash((self.x, self.y))

    def __repr__(self):
        return f"Point({self.x!r}, {self.y!r})"


class Line:
    def __init__(self, start: Point, end: Point):
        self.start = start
        self.end = end

    def length(self) -> float:
        return self.start.distance_to(self.end)

    def contains(self, point: Point) -> bool:
        length = self.length()
        if length <= TOLERANCE:
            return self.start.distance_to(point) <= TOLERANCE
        dx = self.end.x - self.start.x
        dy = self.end.y - self.start.y
        px = point.x - self.start.x
        py = point.y - self.start.y
        # perpendicular distance from the infinite line
        if abs(dx * py - dy * px) / length > TOLERANCE:
            return False
        # projection parameter along the segment, with tolerance in length units
        t = (px * dx + py * dy) / length
        return -TOLERANCE <= t <= length + TOLERANCE
