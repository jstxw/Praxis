import pytest

import iterutils


def test_flatten_still_works():
    assert iterutils.flatten([[1, 2], [3], []]) == [1, 2, 3]


def test_chunked_even_split():
    assert iterutils.chunked([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]


def test_chunked_short_last_chunk():
    assert iterutils.chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


def test_chunked_size_larger_than_input():
    assert iterutils.chunked([1, 2], 10) == [[1, 2]]


def test_chunked_size_one():
    assert iterutils.chunked("abc", 1) == [["a"], ["b"], ["c"]]


def test_chunked_empty():
    assert iterutils.chunked([], 3) == []


def test_chunked_generator_input():
    gen = (i * i for i in range(5))
    assert iterutils.chunked(gen, 2) == [[0, 1], [4, 9], [16]]


def test_chunked_rejects_zero_size():
    with pytest.raises(ValueError):
        iterutils.chunked([1, 2], 0)


def test_chunked_rejects_negative_size():
    with pytest.raises(ValueError):
        iterutils.chunked([1, 2], -1)


def test_chunked_chunks_are_lists():
    result = iterutils.chunked((1, 2, 3), 2)
    assert all(type(chunk) is list for chunk in result)
