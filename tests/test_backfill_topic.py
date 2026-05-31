"""SP-3 Stage 1 — guarded, idempotent topic backfill (memory_lib.backfill_topic).

D4/D11: stamp existing memories to a caller-supplied default topic, leaving
user/feedback operational rows NULL; idempotent via meta.topic_backfill_complete;
audited per row; reversible. Plus no-regression checks for the new nullable
columns on the existing write/read path.

NOTE: every test runs on a tmp DB. The live-data backfill is Peter-gated and is
NEVER executed here.
"""
import pytest

from ultra_memory import memory_lib
from ultra_memory import memory_query
from ultra_memory import memory_export as mx
from ultra_memory import retrieval_core as rc


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def _embedder(texts):
    # Deterministic 3-dim stub (no model download); proportional to text length.
    return [[float(len(t)), 1.0, 0.0] for t in texts]


def _seed(conn):
    for i, typ in enumerate(("project", "reference", "user", "feedback", "project")):
        memory_lib.save_memory(conn, id=f"m{i}", type=typ, title=f"t{i}",
                               body=f"b{i}", ts="2026-05-30T10:00:00")


def test_backfill_stamps_non_operational_rows_only(tmp_path):
    conn = _db(tmp_path)
    _seed(conn)
    summary = memory_lib.backfill_topic(conn, default_topic="trading",
                                        ts="2026-05-31T10:00:00")
    assert summary["stamped"] == 3              # m0, m1, m4 (project/reference/project)
    assert summary["skipped_already_complete"] is False

    by_type = {r["type"]: r["topic"] for r in
               conn.execute("SELECT type, topic FROM memories")}
    # project/reference stamped; user/feedback stay NULL (D11)
    rows = conn.execute("SELECT id, type, topic FROM memories ORDER BY id").fetchall()
    for r in rows:
        if r["type"] in ("user", "feedback"):
            assert r["topic"] is None, r["id"]
        else:
            assert r["topic"] == "trading", r["id"]
    conn.close()


def test_backfill_is_idempotent_via_flag(tmp_path):
    conn = _db(tmp_path)
    _seed(conn)
    first = memory_lib.backfill_topic(conn, default_topic="trading",
                                      ts="2026-05-31T10:00:00")
    assert first["stamped"] == 3
    # Add a NEW non-operational row AFTER the flag is set — a re-run must NOT touch
    # it (the flag short-circuits), proving the flag is the idempotency gate.
    memory_lib.save_memory(conn, id="late", type="project", title="t", body="b",
                           ts="2026-05-31T11:00:00")
    second = memory_lib.backfill_topic(conn, default_topic="trading",
                                       ts="2026-05-31T12:00:00")
    assert second["stamped"] == 0
    assert second["skipped_already_complete"] is True
    assert conn.execute(
        "SELECT topic FROM memories WHERE id='late'").fetchone()[0] is None
    conn.close()


def test_backfill_audits_each_stamped_row(tmp_path):
    conn = _db(tmp_path)
    _seed(conn)
    memory_lib.backfill_topic(conn, default_topic="trading",
                              ts="2026-05-31T10:00:00")
    audits = conn.execute(
        "SELECT target_id FROM audit_log WHERE op='backfill_topic'").fetchall()
    assert {r["target_id"] for r in audits} == {"m0", "m1", "m4"}
    conn.close()


def test_backfill_is_reversible_by_clearing_flag(tmp_path):
    """Reversibility contract: clearing the flag (+ git-export rollback in prod)
    lets the backfill re-run. After clearing topic + flag, a re-run re-stamps."""
    conn = _db(tmp_path)
    _seed(conn)
    memory_lib.backfill_topic(conn, default_topic="trading", ts="2026-05-31T10:00:00")
    # Manual revert (what a git-export restore + flag-clear achieves in prod).
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE memories SET topic=NULL")
    conn.execute("DELETE FROM meta WHERE key='topic_backfill_complete'")
    conn.execute("COMMIT")
    redo = memory_lib.backfill_topic(conn, default_topic="programming",
                                     ts="2026-05-31T13:00:00")
    assert redo["stamped"] == 3
    assert conn.execute(
        "SELECT topic FROM memories WHERE id='m0'").fetchone()[0] == "programming"
    conn.close()


def test_backfill_rejects_empty_default_topic(tmp_path):
    conn = _db(tmp_path)
    _seed(conn)
    with pytest.raises(ValueError):
        memory_lib.backfill_topic(conn, default_topic="", ts="2026-05-31T10:00:00")
    with pytest.raises(ValueError):
        memory_lib.backfill_topic(conn, default_topic=None, ts="2026-05-31T10:00:00")
    conn.close()


def test_backfill_registered_in_replay_spool_dispatch(tmp_path):
    """A spooled backfill must be replayable (registered op). Drive it through the
    spool path directly (no live-busy DB needed)."""
    import json
    conn = _db(tmp_path)
    _seed(conn)
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "bf.json").write_text(json.dumps({
        "op": "backfill_topic", "default_topic": "trading",
        "ts": "2026-05-31T10:00:00", "reason": "replay"}))
    summary = memory_lib.replay_spool(conn, spool_dir=spool)
    assert summary["replayed"] == 1 and summary["failed"] == 0
    assert conn.execute(
        "SELECT topic FROM memories WHERE id='m0'").fetchone()[0] == "trading"
    conn.close()


def test_export_round_trips_topic(tmp_path):
    conn = _db(tmp_path)
    _seed(conn)
    memory_lib.backfill_topic(conn, default_topic="trading", ts="2026-05-31T10:00:00")
    out = tmp_path / "exp"
    assert mx.export_memory(conn, out, ts="2026-05-31T12:00:00") is True
    dump = (out / "memory.dump.sql").read_text()
    # The committed rollback artifact carries the topic value.
    assert "trading" in dump
    conn.close()


def test_topic_change_drives_fresh_export(tmp_path):
    """A topic-only change must NOT be hash-skipped (topic is in _STABLE_COLS)."""
    conn = _db(tmp_path)
    _seed(conn)
    out = tmp_path / "exp"
    assert mx.export_memory(conn, out, ts="2026-05-31T12:00:00") is True
    # second export with no change -> skipped
    assert mx.export_memory(conn, out, ts="2026-05-31T12:05:00") is False
    # backfill changes topic only -> must re-export
    memory_lib.backfill_topic(conn, default_topic="trading", ts="2026-05-31T13:00:00")
    assert mx.export_memory(conn, out, ts="2026-05-31T13:01:00") is True
    conn.close()


def test_save_and_query_no_regression_with_null_topic(tmp_path):
    """save_memory still works (new nullable columns); query_memories returns
    topic-NULL rows exactly as before (no topic filter exists in Stage 1)."""
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="project", title="alpha",
                           body="bull put spread thesis", ts="2026-05-30T10:00:00")
    row = conn.execute(
        "SELECT topic, created_by, outcome_weight FROM memories WHERE id='m1'"
    ).fetchone()
    assert row["topic"] is None
    assert row["created_by"] == "human"
    assert row["outcome_weight"] == 1.0
    results = memory_query.query_memories(conn, "bull put spread", embedder=_embedder,
                                          dim=3, now_ts="2026-05-30T12:00:00")
    assert any(r["id"] == "m1" for r in results)
    conn.close()
