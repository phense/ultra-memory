import pytest

from ultra_memory import memory_lib


def test_open_memory_db_migrates(tmp_path):
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 1
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "memories" in tables and "audit_log" in tables
    conn.close()


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def test_save_memory_creates_row_and_audit(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="feedback-x", type="feedback",
                           title="t", body="b", ts="2026-05-30T10:00:00")
    row = conn.execute("SELECT * FROM memories WHERE id='feedback-x'").fetchone()
    assert row["type"] == "feedback" and row["body"] == "b"
    assert row["created_at"] == "2026-05-30T10:00:00"
    audit = conn.execute("SELECT op, reason FROM audit_log WHERE target_id='feedback-x'").fetchone()
    assert audit["op"] == "save" and audit["reason"] == "create"
    conn.close()


def test_save_memory_redacts_secret_in_body(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="reference", title="t",
                           body="token sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF x",
                           ts="2026-05-30T10:00:00")
    body = conn.execute("SELECT body FROM memories WHERE id='m1'").fetchone()["body"]
    assert "sk-ant" not in body and "[REDACTED]" in body
    conn.close()


def test_save_memory_update_captures_prior(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="reference", title="t",
                           body="v1", ts="2026-05-30T10:00:00")
    memory_lib.save_memory(conn, id="m1", type="reference", title="t",
                           body="v2", ts="2026-05-30T11:00:00")
    row = conn.execute("SELECT body, updated_at FROM memories WHERE id='m1'").fetchone()
    assert row["body"] == "v2" and row["updated_at"] == "2026-05-30T11:00:00"
    reasons = [r[0] for r in conn.execute(
        "SELECT reason FROM audit_log WHERE target_id='m1' ORDER BY id")]
    assert reasons == ["create", "update"]
    conn.close()


def test_record_session_event_creates_session_and_event(tmp_path):
    conn = _db(tmp_path)
    memory_lib.record_session_event(conn, session_id="s1", kind="task_done",
                                    title="did x", ts="2026-05-30T10:00:00")
    assert conn.execute("SELECT 1 FROM sessions WHERE id='s1'").fetchone() is not None
    ev = conn.execute("SELECT kind, title FROM session_events WHERE session_id='s1'").fetchone()
    assert ev["kind"] == "task_done" and ev["title"] == "did x"
    conn.close()


def test_record_session_event_is_idempotent(tmp_path):
    conn = _db(tmp_path)
    for _ in range(2):
        memory_lib.record_session_event(conn, session_id="s1", kind="task_done",
                                        title="did x", ts="2026-05-30T10:00:00")
    n = conn.execute("SELECT COUNT(*) FROM session_events WHERE session_id='s1'").fetchone()[0]
    assert n == 1
    conn.close()


def test_record_access_increments_atomically(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="reference", title="t", body="b",
                           ts="2026-05-30T10:00:00")
    memory_lib.record_access(conn, target_kind="memory", target_id="m1",
                             ts="2026-05-30T10:05:00")
    memory_lib.record_access(conn, target_kind="memory", target_id="m1",
                             ts="2026-05-30T10:06:00")
    row = conn.execute("SELECT access_count, last_accessed FROM memories WHERE id='m1'").fetchone()
    assert row["access_count"] == 2 and row["last_accessed"] == "2026-05-30T10:06:00"
    n = conn.execute("SELECT COUNT(*) FROM access_log WHERE target_id='m1'").fetchone()[0]
    assert n == 2
    conn.close()


def test_record_access_nonmemory_only_logs(tmp_path):
    conn = _db(tmp_path)
    memory_lib.record_access(conn, target_kind="wiki", target_id="slug-x",
                             ts="2026-05-30T10:00:00")
    n = conn.execute("SELECT COUNT(*) FROM access_log WHERE target_id='slug-x'").fetchone()[0]
    assert n == 1
    conn.close()


def test_consolidate_redirects_without_deleting(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="dup", type="reference", title="t", body="b",
                           ts="2026-05-30T10:00:00")
    memory_lib.consolidate(conn, loser_id="dup", canonical_id="canon",
                           reason="duplicate", ts="2026-05-30T11:00:00")
    row = conn.execute("SELECT status, supersedes FROM memories WHERE id='dup'").fetchone()
    assert row["status"] == "redirect" and row["supersedes"] == "canon"
    assert conn.execute("SELECT op FROM audit_log WHERE target_id='dup'").fetchall()[-1][0] == "redirect"
    conn.close()


def test_consolidate_missing_raises(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(KeyError):
        memory_lib.consolidate(conn, loser_id="nope", canonical_id="c",
                               reason="x", ts="2026-05-30T10:00:00")
    conn.close()


def test_delete_soft_tombstones(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="reference", title="t", body="b",
                           ts="2026-05-30T10:00:00")
    memory_lib.delete(conn, id="m1", reason="garbage", tier="volatile",
                      ts="2026-05-30T11:00:00")
    row = conn.execute("SELECT status FROM memories WHERE id='m1'").fetchone()
    assert row["status"] == "deleted"  # row still present (tombstone)
    audit = conn.execute("SELECT op, reason FROM audit_log WHERE target_id='m1'").fetchall()[-1]
    assert audit[0] == "soft_delete" and "volatile" in audit[1]
    conn.close()


def test_delete_unknown_tier_raises(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="reference", title="t", body="b",
                           ts="2026-05-30T10:00:00")
    with pytest.raises(ValueError):
        memory_lib.delete(conn, id="m1", reason="x", tier="bogus",
                          ts="2026-05-30T11:00:00")
    conn.close()


def test_save_memory_persists_fidelity_fields(tmp_path):
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="reference", title="Human Title",
                           body="b", ts="2026-05-30T10:00:00",
                           description="one-liner", index_hook="the hook",
                           node_type="memory")
    row = conn.execute("SELECT description, index_hook, node_type FROM memories "
                       "WHERE id='m1'").fetchone()
    assert row["description"] == "one-liner"
    assert row["index_hook"] == "the hook"
    assert row["node_type"] == "memory"
    conn.close()
