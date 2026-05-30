import json
import sqlite3

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


def test_record_session_event_distinct_detail_no_collision(tmp_path):
    """M1: same session/ts/kind/title but DIFFERENT detail must both persist —
    the event_key must not collide them into one silently-dropped row."""
    conn = _db(tmp_path)
    memory_lib.record_session_event(conn, session_id="s1", kind="note", title="same",
                                    ts="2026-05-30T10:00:00", detail="first")
    memory_lib.record_session_event(conn, session_id="s1", kind="note", title="same",
                                    ts="2026-05-30T10:00:00", detail="second")
    n = conn.execute("SELECT COUNT(*) FROM session_events WHERE session_id='s1'").fetchone()[0]
    assert n == 2
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


def test_write_txn_retries_busy_then_succeeds(tmp_path):
    """H1: a transient 'database is locked' must be retried with backoff, not
    dropped."""
    conn = _db(tmp_path)
    calls = {"n": 0}
    sleeps = []

    def work():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        conn.execute("INSERT INTO meta (key, value) VALUES ('k','v')")

    memory_lib._write_txn(conn, work, retries=5, sleep=lambda s: sleeps.append(s))
    assert calls["n"] == 3
    assert len(sleeps) == 2  # two backoff sleeps before the 3rd attempt succeeds
    assert conn.execute("SELECT value FROM meta WHERE key='k'").fetchone()[0] == "v"
    conn.close()


def test_write_txn_spools_and_raises_on_exhaustion(tmp_path):
    """H1: a write that never gets the lock must be spooled to a durable file AND
    fail loudly — never silently dropped (§6 + §15)."""
    conn = _db(tmp_path)

    def work():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(memory_lib.WriteSpooled):
        memory_lib._write_txn(conn, work, spool={"op": "save_memory", "id": "x"},
                              retries=3, sleep=lambda s: None)
    spooled = list((tmp_path / "memory_spool").glob("*.json"))
    assert len(spooled) == 1
    rec = json.loads(spooled[0].read_text())
    assert rec["op"] == "save_memory" and rec["id"] == "x"
    conn.close()


def test_write_txn_non_busy_error_raises_immediately(tmp_path):
    """H1: a real bug (not a lock) must surface at once, not be retried/spooled."""
    conn = _db(tmp_path)
    calls = {"n": 0}

    def work():
        calls["n"] += 1
        raise ValueError("boom")

    with pytest.raises(ValueError):
        memory_lib._write_txn(conn, work, retries=5, sleep=lambda s: None)
    assert calls["n"] == 1  # no retry on a non-busy error
    assert not (tmp_path / "memory_spool").exists()
    conn.close()


def test_save_memory_routes_through_retry(tmp_path):
    """H1: the public writers must use the retry/spool path, not a bare BEGIN."""
    conn = _db(tmp_path)
    seen = {}
    real = memory_lib._write_txn

    def spy(c, work, **kw):
        seen["spool"] = kw.get("spool")
        return real(c, work, **kw)

    memory_lib._write_txn = spy
    try:
        memory_lib.save_memory(conn, id="m1", type="reference", title="t", body="b",
                               ts="2026-05-30T10:00:00")
    finally:
        memory_lib._write_txn = real
    assert seen["spool"] is not None and seen["spool"]["op"] == "save_memory"
    assert conn.execute("SELECT 1 FROM memories WHERE id='m1'").fetchone() is not None
    conn.close()
