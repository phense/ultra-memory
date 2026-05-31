"""SP-7 engine support — the two GENERIC primitives the Trading-side aggressive
self-improvement loop composes (the loop, its safety wall + eval-gate, and any
"protected row" policy live in the CONSUMER, never here).

Two setters (mirror the set_pinned/set_verified/consolidate shape — _write_txn +
_audit + a spool record + a replay_spool dispatch entry):

  1. set_outcome_weight  — UPDATE memories.outcome_weight (first writer of the 0004
                           column; SP-7's EWMA aggregate writes the regression signal
                           through it).
  2. set_status          — UPDATE memories.status, validated against the known set
                           that ADDS 'quarantined' + 'reverted' to active/redirect/
                           deleted. Raises on an unknown value. Enforces NO "protected
                           row" policy (that is the Trading SP-7 wall).

Plus the recall-exclusion fence: a 'quarantined' / 'reverted' memory must drop out
of query_memories + unified_recall + the rehydrate gist by DEFAULT (the design
relies on "flip status → drops out automatically, no recall-query change").
"""
import sqlite3

import pytest

from ultra_memory import memory_lib, memory_query, unified_query
from ultra_memory.hooks import rehydrate


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def _fake_embedder(mapping, dim=3):
    """First matching substring → its vector; else a zero vector."""
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


# ---------------------------------------------------------------------------
# 1. set_outcome_weight — first writer of the 0004 outcome_weight column.
# ---------------------------------------------------------------------------

def test_set_outcome_weight_writes_non_default(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m", type="project", title="t", body="b",
                           ts="2026-05-01T00:00:00")
    # Default is 1.0 (migration 0004).
    assert conn.execute(
        "SELECT outcome_weight FROM memories WHERE id='m'").fetchone()[0] == 1.0
    memory_lib.set_outcome_weight(conn, id="m", weight=0.42,
                                  ts="2026-05-02T00:00:00")
    assert conn.execute(
        "SELECT outcome_weight FROM memories WHERE id='m'").fetchone()[0] == 0.42
    conn.close()


def test_set_outcome_weight_audited(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m", type="project", title="t", body="b",
                           ts="2026-05-01T00:00:00")
    memory_lib.set_outcome_weight(conn, id="m", weight=1.7,
                                  ts="2026-05-02T00:00:00", reason="ewma aggregate")
    audit = conn.execute(
        "SELECT op, reason FROM audit_log WHERE target_id='m'").fetchall()[-1]
    assert audit[0] == "outcome_weight" and audit[1] == "ewma aggregate"
    conn.close()


def test_set_outcome_weight_missing_id_raises(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(KeyError, match="set_outcome_weight"):
        memory_lib.set_outcome_weight(conn, id="nope", weight=0.5,
                                      ts="2026-05-02T00:00:00")
    conn.close()


def test_set_outcome_weight_routes_through_retry_with_spool(tmp_path):
    """Must use the _write_txn retry/spool path (not a bare BEGIN), with a
    replayable spool record."""
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m", type="project", title="t", body="b",
                           ts="2026-05-01T00:00:00")
    seen = {}
    real = memory_lib._write_txn

    def spy(c, work, **kw):
        seen["spool"] = kw.get("spool")
        return real(c, work, **kw)

    memory_lib._write_txn = spy
    try:
        memory_lib.set_outcome_weight(conn, id="m", weight=0.3,
                                      ts="2026-05-02T00:00:00")
    finally:
        memory_lib._write_txn = real
    assert seen["spool"] is not None
    assert seen["spool"]["op"] == "set_outcome_weight"
    assert seen["spool"]["id"] == "m" and seen["spool"]["weight"] == 0.3
    conn.close()


def test_replay_drains_set_outcome_weight(tmp_path):
    """A spooled set_outcome_weight record replays through replay_spool's dispatch."""
    import hashlib
    import json
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m", type="project", title="t", body="b",
                           ts="2026-05-01T00:00:00")
    sd = tmp_path / "memory_spool"
    sd.mkdir()
    rec = {"op": "set_outcome_weight", "id": "m", "weight": 0.25,
           "ts": "2026-05-02T00:00:00", "reason": "outcome aggregate"}
    payload = json.dumps(rec, sort_keys=True, ensure_ascii=False)
    key = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    (sd / f"{key}.json").write_text(payload, encoding="utf-8")
    s = memory_lib.replay_spool(conn)
    assert s["replayed"] == 1 and s["failed"] == 0, s
    assert conn.execute(
        "SELECT outcome_weight FROM memories WHERE id='m'").fetchone()[0] == 0.25
    assert not list(sd.glob("*.json"))
    conn.close()


# ---------------------------------------------------------------------------
# 2. set_status — flip to quarantined / reverted; reject unknown; audited; spooled.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["quarantined", "reverted", "active",
                                    "redirect", "deleted"])
def test_set_status_accepts_known_statuses(tmp_path, status):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m", type="project", title="t", body="b",
                           ts="2026-05-01T00:00:00")
    memory_lib.set_status(conn, id="m", status=status, ts="2026-05-02T00:00:00",
                          reason="sp7 demotion")
    assert conn.execute(
        "SELECT status FROM memories WHERE id='m'").fetchone()[0] == status
    conn.close()


def test_set_status_rejects_unknown(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m", type="project", title="t", body="b",
                           ts="2026-05-01T00:00:00")
    with pytest.raises(ValueError, match="status"):
        memory_lib.set_status(conn, id="m", status="bogus",
                              ts="2026-05-02T00:00:00", reason="x")
    # The bad value never landed.
    assert conn.execute(
        "SELECT status FROM memories WHERE id='m'").fetchone()[0] == "active"
    conn.close()


def test_set_status_missing_id_raises(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(KeyError, match="set_status"):
        memory_lib.set_status(conn, id="nope", status="quarantined",
                              ts="2026-05-02T00:00:00", reason="x")
    conn.close()


def test_set_status_audited(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m", type="project", title="t", body="b",
                           ts="2026-05-01T00:00:00")
    memory_lib.set_status(conn, id="m", status="quarantined",
                          ts="2026-05-02T00:00:00", reason="contradictory pair")
    audit = conn.execute(
        "SELECT op, reason, prior_state FROM audit_log WHERE target_id='m'"
    ).fetchall()[-1]
    assert audit[0] == "set_status" and audit[1] == "contradictory pair"
    assert audit[2] is not None  # prior captured for reconstruction
    conn.close()


def test_set_status_routes_through_retry_with_spool(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m", type="project", title="t", body="b",
                           ts="2026-05-01T00:00:00")
    seen = {}
    real = memory_lib._write_txn

    def spy(c, work, **kw):
        seen["spool"] = kw.get("spool")
        return real(c, work, **kw)

    memory_lib._write_txn = spy
    try:
        memory_lib.set_status(conn, id="m", status="reverted",
                              ts="2026-05-02T00:00:00", reason="regressed unit")
    finally:
        memory_lib._write_txn = real
    assert seen["spool"] is not None
    assert seen["spool"]["op"] == "set_status"
    assert seen["spool"]["status"] == "reverted" and seen["spool"]["id"] == "m"
    conn.close()


def test_replay_drains_set_status(tmp_path):
    import hashlib
    import json
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m", type="project", title="t", body="b",
                           ts="2026-05-01T00:00:00")
    sd = tmp_path / "memory_spool"
    sd.mkdir()
    rec = {"op": "set_status", "id": "m", "status": "quarantined",
           "ts": "2026-05-02T00:00:00", "reason": "sp7 demotion"}
    payload = json.dumps(rec, sort_keys=True, ensure_ascii=False)
    key = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    (sd / f"{key}.json").write_text(payload, encoding="utf-8")
    s = memory_lib.replay_spool(conn)
    assert s["replayed"] == 1 and s["failed"] == 0, s
    assert conn.execute(
        "SELECT status FROM memories WHERE id='m'").fetchone()[0] == "quarantined"
    assert not list(sd.glob("*.json"))
    conn.close()


# ---------------------------------------------------------------------------
# 3. RECALL-EXCLUSION FENCE (critical): quarantined/reverted drop out of recall
#    by DEFAULT — no recall-query change. query_memories + unified_recall + gist.
# ---------------------------------------------------------------------------

def test_quarantined_and_reverted_excluded_from_query_memories(tmp_path):
    conn = _db(tmp_path)
    emb = _fake_embedder({"alpha": [1.0, 0.0, 0.0]})
    for mid in ("keep", "quar", "rev"):
        memory_lib.save_memory(conn, id=mid, type="reference", title="alpha",
                               body="alpha body", ts="2026-05-01T00:00:00")
    memory_lib.set_status(conn, id="quar", status="quarantined",
                          ts="2026-05-02T00:00:00", reason="x")
    memory_lib.set_status(conn, id="rev", status="reverted",
                          ts="2026-05-02T00:00:00", reason="x")
    out = memory_query.query_memories(conn, "alpha", embedder=emb, dim=3, top_k=10,
                                      now_ts="2026-05-03T00:00:00")
    ids = {r["id"] for r in out}
    assert "keep" in ids
    assert "quar" not in ids and "rev" not in ids
    conn.close()


def test_quarantined_and_reverted_excluded_from_unified_recall(tmp_path):
    conn = _db(tmp_path)
    emb = _fake_embedder({"alpha": [1.0, 0.0, 0.0]})
    for mid in ("keep", "quar", "rev"):
        memory_lib.save_memory(conn, id=mid, type="reference", title="alpha",
                               body="alpha body", ts="2026-05-01T00:00:00")
    memory_lib.set_status(conn, id="quar", status="quarantined",
                          ts="2026-05-02T00:00:00", reason="x")
    memory_lib.set_status(conn, id="rev", status="reverted",
                          ts="2026-05-02T00:00:00", reason="x")
    # Orchestrator (all types), all topics (agent_topics=None); unified_index empty
    # ⇒ memory-only path delegates straight to query_memories' status filter.
    out = unified_query.unified_recall(
        conn, "alpha", caller_class="orchestrator", agent_topics=None,
        embedder=emb, top_k=10, dim=3, now_ts="2026-05-03T00:00:00",
        ts="2026-05-03T00:00:00")
    ids = {r["id"] for r in out}
    assert "keep" in ids
    assert "quar" not in ids and "rev" not in ids
    conn.close()


def test_quarantined_and_reverted_excluded_from_rehydrate_gist(tmp_path):
    conn = _db(tmp_path)
    # Pinned + hot memories both filter on status='active' in build_gist.
    for mid in ("keepers", "quarantined-unit", "reverted-unit"):
        memory_lib.save_memory(conn, id=mid, type="feedback", title=mid,
                               body=f"{mid} body line one", ts="2026-05-01T00:00:00")
        conn.execute("UPDATE memories SET pinned=1 WHERE id=?", (mid,))
    conn.commit()
    memory_lib.set_status(conn, id="quarantined-unit", status="quarantined",
                          ts="2026-05-02T00:00:00", reason="x")
    memory_lib.set_status(conn, id="reverted-unit", status="reverted",
                          ts="2026-05-02T00:00:00", reason="x")
    g = rehydrate.build_gist(conn)
    assert "keepers" in g
    assert "quarantined-unit" not in g and "reverted-unit" not in g
    conn.close()
