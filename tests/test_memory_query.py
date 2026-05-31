import pytest

from ultra_memory import memory_lib, memory_query


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def _fake_embedder(mapping, dim=3):
    """Map the first matching substring → its vector; else a zero vector."""
    def _embed(texts):
        out = []
        for t in texts:
            vec = [0.0] * dim
            for key, v in mapping.items():
                if key in t:
                    vec = v
                    break
            out.append(vec)
        return out
    return _embed


def _save(conn, **kw):
    kw.setdefault("type", "reference")
    kw.setdefault("ts", "2026-05-01T00:00:00")
    memory_lib.save_memory(conn, **kw)


def test_title_hit_is_word_bounded(tmp_path):
    """L1: title-injection must match whole tokens, not substrings — 'car' inside
    'oscar', 'new' inside 'newsletter', 'test' inside 'backtest' are NOT hits."""
    assert memory_query._title_hit("oauth", "the oauth rule") is True
    assert memory_query._title_hit("new", "new ideas today") is True
    assert memory_query._title_hit("new", "the newsletter shipped") is False
    assert memory_query._title_hit("test", "the backtest passed") is False
    assert memory_query._title_hit("car", "oscar predictions") is False


def test_days_between_normalizes_tz_mismatch():
    """L2: a tz-aware vs naive timestamp must be normalized to naive UTC and yield
    the REAL age — not crash, and not silently 0. Returning 0 here used to kill the
    staleness signal in production (tz-aware now vs naive-UTC stored ts)."""
    assert memory_query._days_between(
        "2026-05-30T10:00:00+00:00", "2026-01-01T00:00:00") == 149


def test_query_ranks_by_cosine(tmp_path):
    conn = _db(tmp_path)
    _save(conn, id="apple", title="apple", body="apple fruit")
    _save(conn, id="car", title="car", body="car vehicle")
    emb = _fake_embedder({"apple": [1.0, 0.0, 0.0], "car": [0.0, 1.0, 0.0]})
    out = memory_query.query_memories(conn, "apple", embedder=emb, dim=3,
                                      now_ts="2026-05-02T00:00:00")
    assert out[0]["id"] == "apple"
    assert out[0]["score"] >= out[-1]["score"]
    conn.close()


def test_query_excludes_deleted_and_redirect(tmp_path):
    conn = _db(tmp_path)
    _save(conn, id="live", title="live", body="live one")
    _save(conn, id="gone", title="gone", body="gone one")
    _save(conn, id="moved", title="moved", body="moved one")
    memory_lib.delete(conn, id="gone", reason="x", tier="volatile",
                      ts="2026-05-02T00:00:00")
    memory_lib.consolidate(conn, loser_id="moved", canonical_id="live",
                           reason="dup", ts="2026-05-02T00:00:00")
    emb = _fake_embedder({"live": [1.0, 0.0, 0.0], "gone": [1.0, 0.0, 0.0],
                          "moved": [1.0, 0.0, 0.0]})
    out = memory_query.query_memories(conn, "live", embedder=emb, dim=3,
                                      now_ts="2026-05-02T00:00:00")
    ids = {r["id"] for r in out}
    assert ids == {"live"}
    conn.close()


def test_title_injection_surfaces_weak_embedding(tmp_path):
    conn = _db(tmp_path)
    # Both docs are orthogonal to the query embedding; only 'oauth' has a title
    # that appears in the query, so the title boost must lift it above 'other'.
    _save(conn, id="oauth", title="oauth", body="auth notes")
    _save(conn, id="other", title="other", body="other notes")
    # Insertion order matters for the substring fake: the full query string is
    # listed first so embedding the query resolves to its own vector, not 'oauth'.
    emb = _fake_embedder({"find the oauth rule": [0.0, 0.0, 1.0],  # query vec
                          "oauth": [1.0, 0.0, 0.0],                # orthogonal to query
                          "other": [0.0, 1.0, 0.0]})               # orthogonal to query
    out = memory_query.query_memories(conn, "find the oauth rule", embedder=emb,
                                      dim=3, now_ts="2026-05-02T00:00:00")
    assert out[0]["id"] == "oauth"  # title 'oauth' ∈ query → boost beats 'other'
    conn.close()


def test_query_batches_cache_misses(tmp_path):
    """L7: a query over N uncached memories must embed them in one batched call,
    not one embedder call + write txn per memory."""
    conn = _db(tmp_path)
    for i in range(3):
        _save(conn, id=f"m{i}", title=f"t{i}", body=f"body {i}")
    calls = []

    def emb(texts):
        calls.append(list(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]

    memory_query.query_memories(conn, "q", embedder=emb, dim=3,
                                now_ts="2026-05-02T00:00:00")
    # one batched embed of the 3 misses + one embed of the query = 2 calls (not 4)
    assert len(calls) == 2
    assert any(len(c) == 3 for c in calls)
    conn.close()


def test_query_empty_corpus_returns_empty(tmp_path):
    conn = _db(tmp_path)
    emb = _fake_embedder({})
    assert memory_query.query_memories(conn, "x", embedder=emb, dim=3,
                                       now_ts="2026-05-02T00:00:00") == []
    conn.close()


def test_access_and_strength_boost_changes_order(tmp_path):
    conn = _db(tmp_path)
    _save(conn, id="a", title="a", body="a doc")
    _save(conn, id="b", title="b", body="b doc")
    # Identical embeddings → tie broken by access boost (b accessed more).
    emb = _fake_embedder({"a doc": [1.0, 0.0, 0.0], "b doc": [1.0, 0.0, 0.0],
                          "q": [1.0, 0.0, 0.0]})
    for _ in range(5):
        memory_lib.record_access(conn, target_kind="memory", target_id="b",
                                 ts="2026-05-02T00:00:00")
    out = memory_query.query_memories(conn, "q", embedder=emb, dim=3,
                                      now_ts="2026-05-02T00:00:00")
    assert out[0]["id"] == "b"
    conn.close()


def test_staleness_flag_and_penalty(tmp_path):
    conn = _db(tmp_path)
    _save(conn, id="old", title="old", body="old doc", ts="2026-01-01T00:00:00")
    emb = _fake_embedder({"old doc": [1.0, 0.0, 0.0], "q": [1.0, 0.0, 0.0]})
    out = memory_query.query_memories(conn, "q", embedder=emb, dim=3,
                                      now_ts="2026-05-02T00:00:00", staleness_days=90)
    assert out[0]["stale"] is True
    conn.close()


def test_staleness_penalty_lowers_ranking(tmp_path):
    """L11: the staleness penalty must actually affect the score/ordering — two
    equally-relevant memories rank with the fresh one above the stale one."""
    conn = _db(tmp_path)
    _save(conn, id="fresh", title="fresh", body="same body", ts="2026-04-20T00:00:00")
    _save(conn, id="stale", title="stale", body="same body", ts="2026-01-01T00:00:00")
    emb = _fake_embedder({"same body": [1.0, 0.0, 0.0], "q": [1.0, 0.0, 0.0]})
    out = memory_query.query_memories(conn, "q", embedder=emb, dim=3,
                                      now_ts="2026-05-02T00:00:00", staleness_days=90)
    ids = [r["id"] for r in out]
    assert ids.index("fresh") < ids.index("stale")
    fresh = next(r for r in out if r["id"] == "fresh")
    stale = next(r for r in out if r["id"] == "stale")
    assert fresh["stale"] is False and stale["stale"] is True
    assert stale["score"] < fresh["score"]
    conn.close()


def test_not_stale_when_recent(tmp_path):
    conn = _db(tmp_path)
    _save(conn, id="fresh", title="fresh", body="fresh doc", ts="2026-04-20T00:00:00")
    emb = _fake_embedder({"fresh doc": [1.0, 0.0, 0.0], "q": [1.0, 0.0, 0.0]})
    out = memory_query.query_memories(conn, "q", embedder=emb, dim=3,
                                      now_ts="2026-05-02T00:00:00", staleness_days=90)
    assert out[0]["stale"] is False
    conn.close()


def test_one_hop_links_attached(tmp_path):
    conn = _db(tmp_path)
    _save(conn, id="m", title="m", body="m doc")
    conn.execute(
        "INSERT INTO links (src_kind, src_id, predicate, dst_kind, dst_id) "
        "VALUES ('memory','m','grounded_in','wiki','some-slug')")
    emb = _fake_embedder({"m doc": [1.0, 0.0, 0.0], "q": [1.0, 0.0, 0.0]})
    out = memory_query.query_memories(conn, "q", embedder=emb, dim=3,
                                      now_ts="2026-05-02T00:00:00")
    links = out[0]["links"]
    assert links == [{"predicate": "grounded_in", "dst_kind": "wiki", "dst_id": "some-slug"}]
    conn.close()
