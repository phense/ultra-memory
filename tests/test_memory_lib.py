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
