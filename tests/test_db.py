import sqlite3
from pathlib import Path

import pytest

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


def test_migrate_mirrors_schema_version_in_meta(tmp_path):
    """L8 (§7.3): user_version must be mirrored into meta.schema_version, which
    DOES survive iterdump (unlike PRAGMA user_version), giving the dump + bootstrap
    a queryable version."""
    conn = db.connect(tmp_path / "m.db")
    v = db.migrate(conn, MIG)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert row is not None and int(row[0]) == v
    conn.close()


def test_migrate_recovers_from_half_applied_migration(tmp_path):
    """C3: a migration whose columns exist but whose version never got bumped
    (crash between apply and version-bump) must re-run cleanly, not crash with
    'duplicate column name'."""
    conn = db.connect(tmp_path / "m.db")
    latest = db.migrate(conn, MIG)
    assert latest >= 2
    # Simulate the crash: schema is at latest, but user_version regressed to 1.
    conn.execute("PRAGMA user_version=1")
    # Re-running must not raise (idempotent ADD COLUMN) and must reach latest.
    again = db.migrate(conn, MIG)
    assert again == latest
    assert conn.execute("PRAGMA user_version").fetchone()[0] == latest
    conn.close()


def test_migrate_failed_migration_rolls_back_fully(tmp_path):
    """C3: a migration that fails partway must leave NEITHER a partial schema
    change NOR a bumped version — apply + version-bump are atomic."""
    migdir = tmp_path / "mig"
    migdir.mkdir()
    (migdir / "0001_a.sql").write_text("CREATE TABLE t (a TEXT);")
    conn = db.connect(tmp_path / "m.db")
    assert db.migrate(conn, migdir) == 1
    # 0002: first statement valid, second fatal → the whole migration must roll back.
    (migdir / "0002_bad.sql").write_text(
        "ALTER TABLE t ADD COLUMN good TEXT;\nINSERT INTO does_not_exist VALUES (1);")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(conn, migdir)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1  # version unchanged
    cols = [r[1] for r in conn.execute("PRAGMA table_info(t)")]
    assert "good" not in cols  # partial schema change rolled back
    conn.close()
