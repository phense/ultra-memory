"""R4 FIX 5 — migration 0008 (access_log session_id index, additive).

Verifies: a fresh DB lands at v8 with a `idx_access_log_session` composite index on
access_log(session_id, target_kind); a DB stopped at v7 upgrades cleanly v7->v8;
meta.schema_version mirrors user_version at v8; re-applying is a no-op (idempotent,
IF NOT EXISTS). Additive — CREATE INDEX only, no DROP/RENAME/ALTER.

The attribution query (session-end Stop hook) filters access_log on
`target_kind='memory' AND session_id=? AND rank IS NOT NULL`. Pre-0008 the
never-pruned, fastest-growing access_log had NO index → a full scan per session-end.
"""
from pathlib import Path

from ultra_memory import db

MIG = Path(__file__).resolve().parent.parent / "ultra_memory" / "migrations"


def _indexes(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA index_list({table})")}


def _index_cols(conn, index):
    # PRAGMA index_info → (seqno, cid, name) ordered by position in the index.
    return [r[2] for r in conn.execute(f"PRAGMA index_info({index})")]


def test_fresh_db_lands_at_v8_with_session_index(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    version = db.migrate(conn, MIG)
    assert version == 8
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    assert "idx_access_log_session" in _indexes(conn, "access_log")
    # Composite (session_id, target_kind) — covers the attribution query's filter.
    assert _index_cols(conn, "idx_access_log_session") == ["session_id", "target_kind"]
    conn.close()


def test_meta_schema_version_mirrors_user_version_at_v8(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    v = db.migrate(conn, MIG)
    uv = conn.execute("PRAGMA user_version").fetchone()[0]
    mv = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert v == uv == 8
    assert int(mv) == 8
    conn.close()


def test_db_stopped_at_v7_upgrades_cleanly_to_v8(tmp_path):
    """A DB at v7 (0001-0007) with existing access_log rows takes ONLY 0008 next and
    keeps every row; the index then exists."""
    conn = db.connect(tmp_path / "m.db")
    conn.execute("PRAGMA user_version=0")
    for path in sorted(MIG.glob("000[1-7]_*.sql")):
        for stmt in db._split_statements(path.read_text(encoding="utf-8")):
            db._apply_statement(conn, stmt)
    conn.execute("PRAGMA user_version=7")
    # Seed an access_log row pre-index.
    conn.execute(
        "INSERT INTO access_log (target_kind, target_id, ts, context, "
        "session_id, rank) VALUES ('memory','m1','2026-05-30T00:00:00Z','recall',"
        "'sess-1', 1)")
    assert "idx_access_log_session" not in _indexes(conn, "access_log")

    v = db.migrate(conn, MIG)
    assert v == 8
    assert "idx_access_log_session" in _indexes(conn, "access_log")
    # Row intact.
    assert conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0] == 1
    conn.close()


def test_migrate_is_idempotent_on_replay(tmp_path):
    """A 2nd migrate() against an already-v8 DB is a no-op (IF NOT EXISTS index),
    and a simulated crash (version regressed, index already present) re-applies
    cleanly without error."""
    conn = db.connect(tmp_path / "m.db")
    db.migrate(conn, MIG)
    assert "idx_access_log_session" in _indexes(conn, "access_log")
    # Plain replay.
    assert db.migrate(conn, MIG) == 8
    # Simulated crash: version regressed but index already exists → re-apply OK.
    conn.execute("PRAGMA user_version=7")
    assert db.migrate(conn, MIG) == 8
    assert "idx_access_log_session" in _indexes(conn, "access_log")
    conn.close()
