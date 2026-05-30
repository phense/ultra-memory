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


from ultra_memory import memory_lib


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def _embedder(calls):
    # Records call count so we can assert cache hits skip recompute.
    def _embed(texts):
        calls.append(list(texts))
        return [[float(len(t)), 1.0, 0.0] for t in texts]
    return _embed


def test_get_or_embed_caches_and_reuses(tmp_path):
    conn = _db(tmp_path)
    calls = []
    emb = _embedder(calls)
    v1 = rc.get_or_embed(conn, target_kind="memory", target_id="m1",
                         text="hello", embedder=emb, dim=3)
    v2 = rc.get_or_embed(conn, target_kind="memory", target_id="m1",
                         text="hello", embedder=emb, dim=3)
    assert v1 == pytest.approx(v2)
    assert len(calls) == 1  # second call served from cache
    conn.close()


def test_get_or_embed_recomputes_on_text_change(tmp_path):
    conn = _db(tmp_path)
    calls = []
    emb = _embedder(calls)
    rc.get_or_embed(conn, target_kind="memory", target_id="m1",
                    text="hello", embedder=emb, dim=3)
    rc.get_or_embed(conn, target_kind="memory", target_id="m1",
                    text="hello world", embedder=emb, dim=3)
    assert len(calls) == 2  # content hash changed → recompute
    row = conn.execute("SELECT COUNT(*) FROM embeddings WHERE target_id='m1'").fetchone()[0]
    assert row == 1  # upsert, not a second row
    conn.close()


def test_get_or_embed_dim_invariant_raises(tmp_path):
    conn = _db(tmp_path)
    emb = _embedder([])
    rc.get_or_embed(conn, target_kind="memory", target_id="m1",
                    text="hello", embedder=emb, dim=3)
    with pytest.raises(ValueError):
        rc.get_or_embed(conn, target_kind="memory", target_id="m1",
                        text="hello", embedder=emb, dim=4)  # cached dim is 3
    conn.close()


def test_embed_model_id_single_source(tmp_path):
    """L3: the cache-key model name and the fastembed model id must be ONE canonical
    string, else a vector cached under one is re-embedded when the other is passed."""
    import inspect
    assert inspect.signature(rc.default_embedder).parameters["model_name"].default == rc.EMBED_MODEL
    assert rc.EMBED_MODEL.startswith("BAAI/")  # the real fastembed namespace


def test_get_or_embed_batch_one_call_and_txn(tmp_path):
    """L7: a batch must embed all misses in a single embedder call (and persist in
    one write txn), not one call + one write txn per item."""
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    calls = []

    def emb(texts):
        calls.append(list(texts))
        return [[float(len(t)), 1.0, 0.0] for t in texts]

    items = [("memory", f"m{i}", f"text {i}") for i in range(3)]
    out = rc.get_or_embed_batch(conn, items, embedder=emb, dim=3)
    assert set(out) == {"m0", "m1", "m2"}
    assert len(calls) == 1 and len(calls[0]) == 3  # one batched call of 3
    # second run is fully cached → embedder not called again
    rc.get_or_embed_batch(conn, items, embedder=emb, dim=3)
    assert len(calls) == 1
    conn.close()


def test_default_embedder_without_fastembed_raises_clear(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "fastembed":
            raise ImportError("no fastembed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="fastembed"):
        rc.default_embedder()
