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


def test_recall_access_does_not_reorder_ranking(tmp_path):
    """R4 #8 — the self-reinforcing recall loop is BROKEN: recalling/accessing a memory
    must NOT raise its own relevance ranking (recall -> access_count -> rank -> recall).
    Two equally-relevant memories stay in their cosine/insertion tie-order no matter how
    many times one is accessed; access_count no longer feeds the query score."""
    conn = _db(tmp_path)
    _save(conn, id="a", title="a", body="a doc")
    _save(conn, id="b", title="b", body="b doc")
    emb = _fake_embedder({"a doc": [1.0, 0.0, 0.0], "b doc": [1.0, 0.0, 0.0],
                          "q": [1.0, 0.0, 0.0]})
    baseline = memory_query.query_memories(conn, "q", embedder=emb, dim=3,
                                           now_ts="2026-05-02T00:00:00")
    # Hammer b's access_count — under the old (buggy) boost this would float b to #1.
    for _ in range(50):
        memory_lib.record_access(conn, target_kind="memory", target_id="b",
                                 ts="2026-05-02T00:00:00")
    after = memory_query.query_memories(conn, "q", embedder=emb, dim=3,
                                        now_ts="2026-05-02T00:00:00")
    assert [r["id"] for r in after] == [r["id"] for r in baseline], (
        "access_count must not reorder relevance ranking (loop broken)")
    conn.close()


def test_strength_multiplier_still_boosts_ranking(tmp_path):
    """The strength multiplier (a non-recall-feedback signal) STILL affects ranking —
    only the recall-driven access_count boost was removed by the R4 #8 loop fix."""
    conn = _db(tmp_path)
    _save(conn, id="a", title="a", body="a doc")
    _save(conn, id="b", title="b", body="b doc")
    emb = _fake_embedder({"a doc": [1.0, 0.0, 0.0], "b doc": [1.0, 0.0, 0.0],
                          "q": [1.0, 0.0, 0.0]})
    # Raise b's strength → b outranks a (strength is multiplicative, not recall-fed).
    conn.execute("UPDATE memories SET strength=2.0 WHERE id='b'")
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


def test_query_memories_embedder_none_raises_clear_value_error(tmp_path):
    """R3 FIX 1: the memory backend has NO BM25-only fallback — embedding-cosine is
    the only ranker. Passing embedder=None on a NON-empty store must raise a CLEAR
    ValueError naming the misconfiguration, NOT the cryptic `'NoneType' object is not
    callable` TypeError that surfaced mid-function when q_vec = embedder([query])[0]
    ran unconditionally. (The empty-corpus early-return path is exercised separately.)"""
    conn = _db(tmp_path)
    _save(conn, id="m", title="m", body="m doc")
    with pytest.raises(ValueError) as ei:
        memory_query.query_memories(conn, "m", embedder=None, dim=3,
                                    now_ts="2026-05-02T00:00:00")
    msg = str(ei.value)
    assert "embedder" in msg
    assert "BM25" in msg  # the docstring/message is honest: no BM25-only fallback here
    # And a real embedder still works exactly as before.
    emb = _fake_embedder({"m doc": [1.0, 0.0, 0.0], "m": [1.0, 0.0, 0.0]})
    out = memory_query.query_memories(conn, "m", embedder=emb, dim=3,
                                      now_ts="2026-05-02T00:00:00")
    assert out and out[0]["id"] == "m"
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
    # SP-3 Stage 3: _links_for now also surfaces the cross-store sub-types
    # (src_type/dst_type, migration 0004); both NULL here since the raw INSERT
    # omits them.
    assert links == [{"predicate": "grounded_in", "src_type": None,
                      "dst_kind": "wiki", "dst_id": "some-slug", "dst_type": None}]
    conn.close()


# ---------------------------------------------------------------------------
# R4 FIX 6 — query_memories must call `_links_for` (a per-row SELECT) ONLY for the
# top_k survivors, NOT for the whole candidate set. The pre-fix loop built a result
# dict (incl. _links_for) for EVERY matched row, then sorted + truncated at the end
# → N link-SELECTs to return top_k.
# ---------------------------------------------------------------------------

def _seed_n_with_links(conn, n):
    """Seed n reference memories, each carrying one cross-store link, with distinct
    cosine scores (vec[0] = i/n so ranking is deterministic and id != rank)."""
    emb_map = {}
    for i in range(n):
        mid = f"m{i:02d}"
        # Higher i → higher cosine with the query vec [1,0,0] (vec[0] grows with i).
        vec = [(i + 1) / n, 0.0, 0.0]
        _save(conn, id=mid, title=f"title {i}", body=f"body {i}")
        emb_map[f"body {i}"] = vec
        emb_map[f"title {i}"] = vec
        memory_lib.record_link(
            conn, src_kind="memory", src_id=mid, predicate="relates_to",
            dst_kind="knowledge", dst_id=f"kn-{i}", ts="2026-05-01T00:00:00")
    emb_map["QUERY"] = [1.0, 0.0, 0.0]
    return _fake_embedder(emb_map)


def test_fix6_links_for_called_only_for_top_k(tmp_path, monkeypatch):
    """With N=8 candidates and top_k=3, `_links_for` is invoked ~3 times (the
    survivors), NOT 8 times (the full candidate set)."""
    conn = _db(tmp_path)
    n = 8
    emb = _seed_n_with_links(conn, n)

    calls = {"n": 0}
    real_links_for = memory_query._links_for

    def spy(conn_, mid):
        calls["n"] += 1
        return real_links_for(conn_, mid)

    monkeypatch.setattr(memory_query, "_links_for", spy)
    out = memory_query.query_memories(conn, "QUERY", embedder=emb, dim=3, top_k=3,
                                      now_ts="2026-05-02T00:00:00")
    assert len(out) == 3
    # The link work is bounded to the top_k survivors, not the whole candidate set.
    assert calls["n"] <= 3
    assert calls["n"] < n
    conn.close()


def test_fix6_top_k_rows_and_links_identical_to_full_ranking(tmp_path):
    """Bounding the link work must NOT change WHICH rows rank where, the returned
    dict shape, or the links payload — the top_k output is identical to a reference
    that ranks all then truncates."""
    conn = _db(tmp_path)
    n = 8
    emb = _seed_n_with_links(conn, n)

    # Reference: rank ALL (top_k=n), then truncate to 3 in the test.
    full = memory_query.query_memories(conn, "QUERY", embedder=emb, dim=3, top_k=n,
                                       now_ts="2026-05-02T00:00:00")
    reference = full[:3]
    # Actual: ask for exactly top_k=3 (the optimized path).
    actual = memory_query.query_memories(conn, "QUERY", embedder=emb, dim=3, top_k=3,
                                         now_ts="2026-05-02T00:00:00")
    assert actual == reference
    # Each survivor carries its real link (built only for the survivors).
    for d in actual:
        i = int(d["id"][1:])
        assert d["links"] == [{
            "predicate": "relates_to", "src_type": None,
            "dst_kind": "knowledge", "dst_id": f"kn-{i}", "dst_type": None}]
    conn.close()
