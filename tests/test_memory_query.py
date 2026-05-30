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


def test_query_empty_corpus_returns_empty(tmp_path):
    conn = _db(tmp_path)
    emb = _fake_embedder({})
    assert memory_query.query_memories(conn, "x", embedder=emb, dim=3,
                                       now_ts="2026-05-02T00:00:00") == []
    conn.close()
