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


def test_record_access_writes_session_id(tmp_path):
    """SP-8 substrate: record_access threads an optional session_id into the new
    access_log column; it stays NULL when the kwarg is omitted (backward-compatible)."""
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="reference", title="t", body="b",
                           ts="2026-05-30T10:00:00")
    memory_lib.record_access(conn, target_kind="memory", target_id="m1",
                             ts="2026-05-30T10:05:00", session_id="S-abc")
    memory_lib.record_access(conn, target_kind="memory", target_id="m1",
                             ts="2026-05-30T10:06:00")  # no session_id -> NULL
    rows = conn.execute(
        "SELECT session_id FROM access_log WHERE target_id='m1' ORDER BY id"
    ).fetchall()
    assert rows[0]["session_id"] == "S-abc"
    assert rows[1]["session_id"] is None
    conn.close()


def test_record_access_spools_and_replays_session_id(tmp_path):
    """SP-8 substrate: a record_access spool record carries session_id, and
    replay_spool re-applies it into the column (the SQLITE_BUSY-casualty path)."""
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="reference", title="t", body="b",
                           ts="2026-05-30T10:00:00")
    spool = memory_lib._spool_dir(conn)
    spool.mkdir(parents=True, exist_ok=True)
    rec = {"op": "record_access", "target_kind": "memory", "target_id": "m1",
           "ts": "2026-05-30T10:07:00", "context": "unified_recall:subagent",
           "session_id": "S-spooled"}
    (spool / "rec.json").write_text(json.dumps(rec), encoding="utf-8")
    summary = memory_lib.replay_spool(conn)
    assert summary["replayed"] == 1 and summary["failed"] == 0
    row = conn.execute(
        "SELECT session_id FROM access_log WHERE target_id='m1'").fetchone()
    assert row["session_id"] == "S-spooled"
    conn.close()


def test_session_id_from_env_reads_var(tmp_path):
    """SP-8 substrate: session_id_from_env returns the stripped
    ULTRA_MEMORY_SESSION_ID, or None when unset/blank (generic, no Trading concept)."""
    assert memory_lib.session_id_from_env({}) is None
    assert memory_lib.session_id_from_env({"ULTRA_MEMORY_SESSION_ID": "  "}) is None
    assert memory_lib.session_id_from_env(
        {"ULTRA_MEMORY_SESSION_ID": "  S-99 "}) == "S-99"


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


def test_consolidate_missing_has_descriptive_message(tmp_path):
    """Nit: the KeyError must name the operation + id, not just bare-raise the id."""
    conn = _db(tmp_path)
    with pytest.raises(KeyError, match="consolidate"):
        memory_lib.consolidate(conn, loser_id="nope", canonical_id="c", reason="x",
                               ts="2026-05-30T10:00:00")
    conn.close()


def test_delete_missing_has_descriptive_message(tmp_path):
    conn = _db(tmp_path)
    with pytest.raises(KeyError, match="delete"):
        memory_lib.delete(conn, id="nope", reason="x", tier="volatile",
                          ts="2026-05-30T10:00:00")
    conn.close()


def test_write_rolls_back_partial_on_error(tmp_path, monkeypatch):
    """L4: a write failing mid-transaction must leave no partial row and the
    connection must stay usable (rollback)."""
    conn = _db(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(memory_lib, "_audit", boom)
    with pytest.raises(RuntimeError):
        memory_lib.save_memory(conn, id="m1", type="reference", title="t", body="b",
                               ts="2026-05-30T10:00:00")
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id='m1'").fetchone()[0] == 0
    monkeypatch.undo()
    memory_lib.save_memory(conn, id="m2", type="reference", title="t", body="b",
                           ts="2026-05-30T10:00:00")  # connection still usable
    assert conn.execute("SELECT COUNT(*) FROM memories WHERE id='m2'").fetchone()[0] == 1
    conn.close()


def test_concurrent_access_increments_no_lost_update(tmp_path):
    """L4: N threads each opening their own connection and incrementing the same
    memory's access_count must total N — no lost updates (WAL + busy_timeout +
    atomic SQL increment + retry)."""
    import threading

    dbp = tmp_path / "m.db"
    conn = memory_lib.open_memory_db(dbp)
    memory_lib.save_memory(conn, id="m1", type="reference", title="t", body="b",
                           ts="2026-05-30T10:00:00")
    conn.close()

    n = 20
    errors = []

    def worker():
        c = memory_lib.open_memory_db(dbp)
        try:
            memory_lib.record_access(c, target_kind="memory", target_id="m1",
                                     ts="2026-05-30T10:05:00")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    final = memory_lib.open_memory_db(dbp)
    count = final.execute("SELECT access_count FROM memories WHERE id='m1'").fetchone()[0]
    final.close()
    assert count == n


def test_session_event_redacts_secret_in_detail(tmp_path):
    """L10: session-event title/detail (the .remember import path) must pass through
    the redaction chokepoint."""
    conn = _db(tmp_path)
    memory_lib.record_session_event(
        conn, session_id="s1", kind="note", title="t", ts="2026-05-30T10:00:00",
        detail="token sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF tail")
    detail = conn.execute(
        "SELECT detail FROM session_events WHERE session_id='s1'").fetchone()[0]
    assert "sk-ant" not in detail and "[REDACTED]" in detail
    conn.close()


def test_resave_preserves_deleted_tombstone(tmp_path):
    """M7: a re-save (e.g. a re-import while the file still exists on disk) must NOT
    resurrect a deliberately-deleted memory — content updates, tombstone stays."""
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="m1", type="reference", title="t", body="b",
                           ts="2026-05-30T10:00:00")
    memory_lib.delete(conn, id="m1", reason="x", tier="volatile",
                      ts="2026-05-30T11:00:00")
    memory_lib.save_memory(conn, id="m1", type="reference", title="t", body="b2",
                           ts="2026-05-30T12:00:00")
    row = conn.execute("SELECT status, body FROM memories WHERE id='m1'").fetchone()
    assert row["status"] == "deleted"  # tombstone preserved
    assert row["body"] == "b2"          # content still updated
    conn.close()


def test_resave_preserves_redirect_status(tmp_path):
    """M7: re-saving a consolidated (redirected) memory keeps the redirect, so a
    re-import can't silently un-merge it."""
    conn = _db(tmp_path)
    memory_lib.save_memory(conn, id="dup", type="reference", title="t", body="b",
                           ts="2026-05-30T10:00:00")
    memory_lib.consolidate(conn, loser_id="dup", canonical_id="canon",
                           reason="d", ts="2026-05-30T11:00:00")
    memory_lib.save_memory(conn, id="dup", type="reference", title="t", body="b2",
                           ts="2026-05-30T12:00:00")
    row = conn.execute("SELECT status, supersedes FROM memories WHERE id='dup'").fetchone()
    assert row["status"] == "redirect" and row["supersedes"] == "canon"
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
