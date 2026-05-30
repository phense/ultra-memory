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
