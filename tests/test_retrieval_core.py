import math

import pytest

from ultra_memory import retrieval_core as rc


def test_cosine_identical_is_one():
    assert rc.cosine([1.0, 0.0, 0.0], [2.0, 0.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert rc.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_zero_vector_is_zero_not_nan():
    val = rc.cosine([0.0, 0.0], [1.0, 1.0])
    assert val == 0.0 and not math.isnan(val)


def test_cosine_search_ranks_desc_and_top_k():
    q = [1.0, 0.0]
    items = [("a", [1.0, 0.0]), ("b", [0.0, 1.0]), ("c", [0.9, 0.1])]
    ranked = rc.cosine_search(q, items, top_k=2)
    assert [i for i, _ in ranked] == ["a", "c"]
    assert ranked[0][1] >= ranked[1][1]
