"""SP-3 Stage 3 — the `links` writer (`record_link`) + the cross-store link-mirror
(§5.5, D5/D6).

`record_link` is the FIRST writer the `links` table ever gets (north-star Risk
§14.8: defined + read but never written). It must:
  - write through `_write_txn` + `_audit`,
  - be idempotent on the edge key (src_kind, src_id, predicate, dst_kind, dst_id) —
    a re-record is an upsert/no-op, never a duplicate row,
  - replay from the durable spool (registered in `replay_spool`'s dispatch),
  - persist the new `src_type`/`dst_type` sub-types (migration 0004).

`_links_for` (the only existing reader, never run against rows until now) must
surface the new `src_type`/`dst_type`.

The link-mirror lifts CROSS-STORE wiki edges (an edge where a wiki node references a
memory or a memory references a wiki node) into `links` via `record_link`. It is fed
the wiki edges as an INPUT iterable (consumer-provided) — it never opens / imports
the wiki's graph.sqlite — so the engine stays project-agnostic. It must upsert only
the cross-store edges it is fed (idempotent) and never touch a pure wiki<->wiki edge
(it is simply never fed one).
"""
import json

from ultra_memory import memory_lib, memory_query


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def _link_rows(conn):
    return conn.execute(
        "SELECT src_kind, src_id, src_type, predicate, dst_kind, dst_id, dst_type, "
        "evidence, confidence, created_at FROM links ORDER BY rowid").fetchall()


# ---------------------------------------------------------------------------
# 1. record_link writes a row + an audit line.
# ---------------------------------------------------------------------------

def test_record_link_writes_row_and_audit(tmp_path):
    conn = _db(tmp_path)
    memory_lib.record_link(
        conn, src_kind="memory", src_id="m1", predicate="validated_as",
        dst_kind="knowledge", dst_id="german-tax-fence",
        src_type="feedback", dst_type="mechanism",
        evidence="seen in 3 sessions", confidence=0.9,
        ts="2026-05-31T10:00:00")
    rows = _link_rows(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["src_kind"] == "memory" and r["src_id"] == "m1"
    assert r["dst_kind"] == "knowledge" and r["dst_id"] == "german-tax-fence"
    assert r["predicate"] == "validated_as"
    assert r["src_type"] == "feedback" and r["dst_type"] == "mechanism"
    assert r["evidence"] == "seen in 3 sessions" and r["confidence"] == 0.9
    assert r["created_at"] == "2026-05-31T10:00:00"

    audit = conn.execute(
        "SELECT op, target_kind, target_id FROM audit_log "
        "WHERE op='link'").fetchone()
    assert audit is not None
    assert audit["target_kind"] == "memory" and audit["target_id"] == "m1"
    conn.close()


def test_record_link_optional_fields_default_none(tmp_path):
    conn = _db(tmp_path)
    memory_lib.record_link(
        conn, src_kind="memory", src_id="m1", predicate="relates_to",
        dst_kind="memory", dst_id="m2", ts="2026-05-31T10:00:00")
    r = _link_rows(conn)[0]
    assert r["src_type"] is None and r["dst_type"] is None
    assert r["evidence"] is None and r["confidence"] is None
    conn.close()


# ---------------------------------------------------------------------------
# 2. Idempotent on the edge key — re-record is an upsert / no-op, not a dup.
# ---------------------------------------------------------------------------

def test_record_link_idempotent_on_edge_key(tmp_path):
    conn = _db(tmp_path)
    for _ in range(3):
        memory_lib.record_link(
            conn, src_kind="memory", src_id="m1", predicate="validated_as",
            dst_kind="knowledge", dst_id="page-a",
            src_type="feedback", dst_type="mechanism",
            ts="2026-05-31T10:00:00")
    assert len(_link_rows(conn)) == 1
    conn.close()


def test_record_link_distinct_predicate_is_distinct_edge(tmp_path):
    conn = _db(tmp_path)
    memory_lib.record_link(
        conn, src_kind="memory", src_id="m1", predicate="validated_as",
        dst_kind="knowledge", dst_id="page-a", ts="2026-05-31T10:00:00")
    memory_lib.record_link(
        conn, src_kind="memory", src_id="m1", predicate="relates_to",
        dst_kind="knowledge", dst_id="page-a", ts="2026-05-31T10:00:00")
    assert len(_link_rows(conn)) == 2
    conn.close()


def test_record_link_upsert_refreshes_metadata(tmp_path):
    """A re-record with the SAME edge key but new evidence/confidence/sub-types
    updates the existing row in place (upsert), still one row."""
    conn = _db(tmp_path)
    memory_lib.record_link(
        conn, src_kind="memory", src_id="m1", predicate="validated_as",
        dst_kind="knowledge", dst_id="page-a", confidence=0.5,
        ts="2026-05-31T10:00:00")
    memory_lib.record_link(
        conn, src_kind="memory", src_id="m1", predicate="validated_as",
        dst_kind="knowledge", dst_id="page-a", confidence=0.95,
        src_type="feedback", dst_type="mechanism",
        evidence="reconfirmed", ts="2026-05-31T11:00:00")
    rows = _link_rows(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["confidence"] == 0.95
    assert r["evidence"] == "reconfirmed"
    assert r["src_type"] == "feedback" and r["dst_type"] == "mechanism"
    conn.close()


# ---------------------------------------------------------------------------
# 3. Replays from the durable spool (registered in replay_spool dispatch).
# ---------------------------------------------------------------------------

def test_record_link_replays_from_spool(tmp_path):
    conn = _db(tmp_path)
    sd = tmp_path / "memory_spool"
    sd.mkdir()
    rec = {"op": "record_link", "src_kind": "memory", "src_id": "m1",
           "predicate": "validated_as", "dst_kind": "knowledge", "dst_id": "page-a",
           "src_type": "feedback", "dst_type": "mechanism",
           "evidence": None, "confidence": None, "ts": "2026-05-31T10:00:00"}
    payload = json.dumps(rec, sort_keys=True, ensure_ascii=False)
    import hashlib
    key = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    (sd / f"{key}.json").write_text(payload, encoding="utf-8")

    s = memory_lib.replay_spool(conn)
    assert s["replayed"] == 1 and s["failed"] == 0, s
    rows = _link_rows(conn)
    assert len(rows) == 1 and rows[0]["dst_id"] == "page-a"
    assert not list(sd.glob("*.json"))  # drained
    conn.close()


def test_record_link_replay_is_idempotent(tmp_path):
    """A spooled link whose edge already exists replays as a no-op (one row)."""
    conn = _db(tmp_path)
    memory_lib.record_link(
        conn, src_kind="memory", src_id="m1", predicate="validated_as",
        dst_kind="knowledge", dst_id="page-a", ts="2026-05-31T10:00:00")
    sd = tmp_path / "memory_spool"
    sd.mkdir()
    rec = {"op": "record_link", "src_kind": "memory", "src_id": "m1",
           "predicate": "validated_as", "dst_kind": "knowledge", "dst_id": "page-a",
           "src_type": None, "dst_type": None,
           "evidence": None, "confidence": None, "ts": "2026-05-31T12:00:00"}
    import hashlib
    payload = json.dumps(rec, sort_keys=True, ensure_ascii=False)
    key = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    (sd / f"{key}.json").write_text(payload, encoding="utf-8")

    s = memory_lib.replay_spool(conn)
    assert s["replayed"] == 1, s
    assert len(_link_rows(conn)) == 1
    conn.close()


# ---------------------------------------------------------------------------
# 4. _links_for surfaces the new src_type/dst_type.
# ---------------------------------------------------------------------------

def test_links_for_surfaces_sub_types(tmp_path):
    conn = _db(tmp_path)
    memory_lib.record_link(
        conn, src_kind="memory", src_id="m1", predicate="validated_as",
        dst_kind="knowledge", dst_id="page-a",
        src_type="feedback", dst_type="mechanism", ts="2026-05-31T10:00:00")
    links = memory_query._links_for(conn, "m1")
    assert len(links) == 1
    lk = links[0]
    assert lk["predicate"] == "validated_as"
    assert lk["dst_kind"] == "knowledge" and lk["dst_id"] == "page-a"
    assert lk["src_type"] == "feedback"
    assert lk["dst_type"] == "mechanism"
    conn.close()


def test_links_for_only_memory_src(tmp_path):
    """_links_for reads memory-source edges only (it is the memory read path)."""
    conn = _db(tmp_path)
    memory_lib.record_link(
        conn, src_kind="memory", src_id="m1", predicate="p",
        dst_kind="knowledge", dst_id="k1", ts="2026-05-31T10:00:00")
    memory_lib.record_link(
        conn, src_kind="knowledge", src_id="k2", predicate="p",
        dst_kind="memory", dst_id="m1", ts="2026-05-31T10:00:00")
    links = memory_query._links_for(conn, "m1")
    assert len(links) == 1 and links[0]["dst_id"] == "k1"
    conn.close()


# ---------------------------------------------------------------------------
# 5. link-mirror — consumer-fed cross-store edges only, idempotent, agnostic.
# ---------------------------------------------------------------------------

def _wiki_edge(subject, predicate, object_, *, subject_kind, object_kind,
               subject_type=None, object_type=None, evidence=None, confidence=None):
    """Shape a consumer-provided wiki-graph edge (the input contract of the mirror).
    The consumer (Trading) reads these out of its OWN graph.sqlite and hands them in;
    the engine never opens that DB."""
    return {
        "src_kind": subject_kind, "src_id": subject, "src_type": subject_type,
        "predicate": predicate,
        "dst_kind": object_kind, "dst_id": object_, "dst_type": object_type,
        "evidence": evidence, "confidence": confidence,
    }


def test_mirror_lifts_only_cross_store_edges(tmp_path):
    conn = _db(tmp_path)
    edges = [
        # cross-store: a memory references a wiki page
        _wiki_edge("m1", "validated_as", "page-a",
                   subject_kind="memory", object_kind="knowledge",
                   subject_type="feedback", object_type="mechanism"),
        # cross-store: a wiki page references a memory
        _wiki_edge("page-b", "cites", "m2",
                   subject_kind="knowledge", object_kind="memory"),
    ]
    summary = memory_lib.mirror_cross_store_links(
        conn, edges, ts="2026-05-31T10:00:00")
    assert summary["mirrored"] == 2
    rows = _link_rows(conn)
    assert len(rows) == 2
    dst_ids = {r["dst_id"] for r in rows}
    assert dst_ids == {"page-a", "m2"}
    conn.close()


def test_mirror_skips_pure_wiki_to_wiki(tmp_path):
    """A pure wiki<->wiki edge that slips into the fed iterable is NOT mirrored —
    the mirror only lifts edges that cross stores (touch a memory). (In practice the
    consumer never feeds one; this asserts defense-in-depth.)"""
    conn = _db(tmp_path)
    edges = [
        _wiki_edge("page-a", "relates_to", "page-b",
                   subject_kind="knowledge", object_kind="knowledge"),
        _wiki_edge("m1", "validated_as", "page-c",
                   subject_kind="memory", object_kind="knowledge"),
    ]
    summary = memory_lib.mirror_cross_store_links(
        conn, edges, ts="2026-05-31T10:00:00")
    assert summary["mirrored"] == 1
    assert summary["skipped_wiki_internal"] == 1
    rows = _link_rows(conn)
    assert len(rows) == 1 and rows[0]["dst_id"] == "page-c"
    conn.close()


def test_mirror_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    edges = [
        _wiki_edge("m1", "validated_as", "page-a",
                   subject_kind="memory", object_kind="knowledge"),
    ]
    memory_lib.mirror_cross_store_links(conn, edges, ts="2026-05-31T10:00:00")
    memory_lib.mirror_cross_store_links(conn, edges, ts="2026-05-31T11:00:00")
    assert len(_link_rows(conn)) == 1
    conn.close()


def test_mirror_empty_iterable_is_noop(tmp_path):
    conn = _db(tmp_path)
    summary = memory_lib.mirror_cross_store_links(conn, [], ts="2026-05-31T10:00:00")
    assert summary == {"mirrored": 0, "skipped_wiki_internal": 0}
    assert _link_rows(conn) == []
    conn.close()


def test_mirror_takes_a_generator(tmp_path):
    """The wiki-edge source is an arbitrary iterable (consumer-fed) — a generator
    works, proving the mirror does not require a list / a DB handle."""
    conn = _db(tmp_path)

    def gen():
        yield _wiki_edge("m1", "validated_as", "page-a",
                         subject_kind="memory", object_kind="knowledge")

    summary = memory_lib.mirror_cross_store_links(
        conn, gen(), ts="2026-05-31T10:00:00")
    assert summary["mirrored"] == 1
    conn.close()
