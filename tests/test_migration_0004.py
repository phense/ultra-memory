"""SP-3 Stage 1 — migration 0004 (cross-store fabric, additive/non-destructive).

Verifies: fresh DB lands at v4 with the new columns/tables/index; a DB stopped at
v3 upgrades cleanly v3->v4; meta.schema_version mirrors user_version; re-applying
is a no-op (replay-tolerant) including after a simulated crash (version regressed,
columns already present). All non-destructive DDL — no DROP/RENAME.
"""
from pathlib import Path

from ultra_memory import db

MIG = Path(__file__).resolve().parent.parent / "ultra_memory" / "migrations"

_NEW_MEMORIES_COLS = ("topic", "created_by", "outcome_weight")
_NEW_LINKS_COLS = ("src_type", "dst_type")
_NEW_TABLES = ("unified_index", "knowledge_pins", "agent_topic_bindings")


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}


def test_fresh_db_lands_at_v4_with_all_additive_schema(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    version = db.migrate(conn, MIG)
    assert version == 4

    mcols = _cols(conn, "memories")
    for c in _NEW_MEMORIES_COLS:
        assert c in mcols, c
    assert "outcome_signal" in _cols(conn, "session_events")
    lcols = _cols(conn, "links")
    for c in _NEW_LINKS_COLS:
        assert c in lcols, c

    tables = _tables(conn)
    for t in _NEW_TABLES:
        assert t in tables, t
    idx = _indexes(conn)
    assert "idx_unified_topic" in idx
    assert "idx_links_dst" in idx
    conn.close()


def test_v4_defaults_match_decisions(tmp_path):
    """D1/D16: topic is nullable (NULL = cross-topic); created_by defaults 'human'
    (safe-immutable); outcome_weight defaults 1.0 (inert in ranking)."""
    conn = db.connect(tmp_path / "m.db")
    db.migrate(conn, MIG)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO memories (id, type, title, body) VALUES (?,?,?,?)",
                 ("m1", "project", "t", "b"))
    conn.execute("COMMIT")
    row = conn.execute(
        "SELECT topic, created_by, outcome_weight FROM memories WHERE id='m1'"
    ).fetchone()
    assert row["topic"] is None
    assert row["created_by"] == "human"
    assert row["outcome_weight"] == 1.0
    conn.close()


def test_meta_schema_version_mirrors_user_version_at_v4(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    v = db.migrate(conn, MIG)
    uv = conn.execute("PRAGMA user_version").fetchone()[0]
    mv = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert v == uv == 4
    assert int(mv) == 4
    conn.close()


def test_db_stopped_at_v3_upgrades_cleanly_to_v4(tmp_path):
    """A DB that exists at v3 (only 0001-0003 applied) must take ONLY 0004 next."""
    conn = db.connect(tmp_path / "m.db")
    conn.execute("PRAGMA user_version=0")
    # Apply 0001-0003 only.
    for path in sorted(MIG.glob("000[123]_*.sql")):
        for stmt in db._split_statements(path.read_text(encoding="utf-8")):
            db._apply_statement(conn, stmt)
    conn.execute("PRAGMA user_version=3")
    assert "topic" not in _cols(conn, "memories")  # precondition: genuinely v3-shaped

    v = db.migrate(conn, MIG)
    assert v == 4
    assert "topic" in _cols(conn, "memories")
    assert "unified_index" in _tables(conn)
    conn.close()


def test_reapply_is_noop(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    v1 = db.migrate(conn, MIG)
    v2 = db.migrate(conn, MIG)  # replay
    assert v1 == v2 == 4
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    conn.close()


def test_replay_tolerant_after_simulated_crash(tmp_path):
    """Columns already present but user_version regressed (crash between apply and
    bump) must re-run cleanly (idempotent ADD COLUMN), not 'duplicate column name'."""
    conn = db.connect(tmp_path / "m.db")
    assert db.migrate(conn, MIG) == 4
    conn.execute("PRAGMA user_version=3")  # simulate crash mid-0004
    again = db.migrate(conn, MIG)
    assert again == 4
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    conn.close()
