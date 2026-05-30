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


def test_rrf_fuse_rewards_agreement():
    # 'x' is top of both lists; 'y' and 'z' each appear once → 'x' wins.
    fused = rc.rrf_fuse([["x", "y"], ["x", "z"]], k=60)
    assert fused[0][0] == "x"
    assert {i for i, _ in fused} == {"x", "y", "z"}


def test_rrf_fuse_score_uses_k():
    fused = dict(rc.rrf_fuse([["a"]], k=60))
    assert fused["a"] == pytest.approx(1.0 / 61)


def test_pack_unpack_roundtrip():
    vec = [0.5, -1.25, 3.0]
    blob = rc.pack_vector(vec)
    assert isinstance(blob, bytes)
    back = rc.unpack_vector(blob, dim=3)
    assert back == pytest.approx(vec)


def test_content_sha256_stable_and_none_safe():
    assert rc.content_sha256("abc") == rc.content_sha256("abc")
    assert rc.content_sha256("abc") != rc.content_sha256("abd")
    assert isinstance(rc.content_sha256(None), str)  # None-safe
