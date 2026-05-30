from pathlib import Path
from ultra_memory import db

MIG = Path(__file__).resolve().parent.parent / "ultra_memory" / "migrations"


def test_connect_sets_wal_and_busy_timeout(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    assert conn.isolation_level is None
    conn.close()


def test_migrate_brings_fresh_db_to_latest(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    version = db.migrate(conn, MIG)
    assert version >= 1
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    for t in ("memories", "sessions", "session_events", "procedures",
              "links", "embeddings", "access_log", "audit_log", "meta"):
        assert t in tables
    conn.close()


def test_migrate_is_idempotent(tmp_path):
    p = tmp_path / "m.db"
    conn = db.connect(p)
    v1 = db.migrate(conn, MIG)
    v2 = db.migrate(conn, MIG)  # re-run: no-op
    assert v1 == v2
    assert conn.execute("PRAGMA user_version").fetchone()[0] == v2
    conn.close()


def test_memories_pk_is_slug_and_pinned_defaults_zero(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    db.migrate(conn, MIG)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO memories (id, type, title, body) VALUES (?,?,?,?)",
        ("feedback-x", "feedback", "t", "b"),
    )
    conn.execute("COMMIT")
    row = conn.execute("SELECT pinned, status FROM memories WHERE id='feedback-x'").fetchone()
    assert row["pinned"] == 0
    assert row["status"] == "active"
    conn.close()
