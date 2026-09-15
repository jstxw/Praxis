import pytest

import intervals


def test_overlaps_unchanged():
    assert intervals.overlaps((1, 3), (3, 4))
    assert not intervals.overlaps((1, 2), (3, 4))


def test_merge_empty():
    assert intervals.merge_intervals([]) == []


def test_merge_single():
    assert intervals.merge_intervals([(2, 5)]) == [(2, 5)]


def test_merge_disjoint_sorted():
    assert intervals.merge_intervals([(5, 6), (1, 2)]) == [(1, 2), (5, 6)]


def test_merge_overlapping():
    assert intervals.merge_intervals([(1, 4), (2, 6), (8, 10)]) == [(1, 6), (8, 10)]


def test_merge_touching_endpoints():
    assert intervals.merge_intervals([(1, 3), (3, 5)]) == [(1, 5)]


def test_merge_contained_interval():
    assert intervals.merge_intervals([(1, 10), (2, 3), (4, 5)]) == [(1, 10)]


def test_merge_unsorted_chain():
    assert intervals.merge_intervals([(7, 9), (1, 2), (2, 4), (4, 7)]) == [(1, 9)]


def test_merge_does_not_extend_end_backwards():
    # a later-starting but shorter interval must not shrink the merged end
    assert intervals.merge_intervals([(1, 10), (2, 5), (11, 12)]) == [(1, 10), (11, 12)]


def test_merge_does_not_mutate_input():
    data = [(5, 6), (1, 2), (2, 3)]
    intervals.merge_intervals(data)
    assert data == [(5, 6), (1, 2), (2, 3)]


def test_merge_returns_tuples_for_list_input():
    result = intervals.merge_intervals([[3, 4], [1, 2]])
    assert result == [(1, 2), (3, 4)]
    assert all(type(item) is tuple for item in result)


def test_merge_point_intervals():
    assert intervals.merge_intervals([(2, 2), (2, 2), (3, 3)]) == [(2, 2), (3, 3)]


def test_merge_negative_and_float_bounds():
    assert intervals.merge_intervals([(-1.5, 0.0), (0.0, 0.5)]) == [(-1.5, 0.5)]


def test_merge_rejects_reversed_interval():
    with pytest.raises(ValueError):
        intervals.merge_intervals([(1, 2), (5, 4)])
