from ultra_memory import memory_lib


def test_open_memory_db_migrates(tmp_path):
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 1
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "memories" in tables and "audit_log" in tables
    conn.close()
