"""SP-6 #6 (D11) — migration 0005 (unified_index.bm25_text, additive).

Verifies: a fresh DB lands at v5 with the new `unified_index.bm25_text` column;
a DB stopped at v4 upgrades cleanly v4->v5 with existing rows intact;
meta.schema_version mirrors user_version at v5; re-applying is a no-op (replay-
tolerant) including after a simulated crash (version regressed, column present).
All non-destructive DDL — ADD COLUMN only.
"""
from pathlib import Path

from ultra_memory import db

MIG = Path(__file__).resolve().parent.parent / "ultra_memory" / "migrations"


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_db_lands_at_v5_with_bm25_text(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    version = db.migrate(conn, MIG)
    assert version >= 5  # 0005 applied (terminal version may be later, e.g. 0006)
    assert "bm25_text" in _cols(conn, "unified_index")
    # The 0004 columns are still there (additive, non-destructive).
    assert "snippet" in _cols(conn, "unified_index")
    assert "content_sha256" in _cols(conn, "unified_index")
    conn.close()


def test_meta_schema_version_mirrors_user_version_at_v5(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    v = db.migrate(conn, MIG)
    uv = conn.execute("PRAGMA user_version").fetchone()[0]
    mv = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    assert v == uv >= 5  # terminal version (>=5; 0006 bumps it further)
    assert int(mv) == uv
    conn.close()


def test_db_stopped_at_v4_upgrades_cleanly_to_v5_rows_intact(tmp_path):
    """A DB at v4 (0001-0004) with an existing unified_index row must take ONLY
    0005 next and keep the row (bm25_text NULL for the un-migrated row)."""
    conn = db.connect(tmp_path / "m.db")
    conn.execute("PRAGMA user_version=0")
    for path in sorted(MIG.glob("000[1234]_*.sql")):
        for stmt in db._split_statements(path.read_text(encoding="utf-8")):
            db._apply_statement(conn, stmt)
    conn.execute("PRAGMA user_version=4")
    assert "bm25_text" not in _cols(conn, "unified_index")  # genuinely v4-shaped
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO unified_index (slug, topic, title, snippet) "
        "VALUES ('s1','trading','T','snip')")
    conn.execute("COMMIT")

    v = db.migrate(conn, MIG)
    assert v >= 5  # 0005 (and any later additive migration) applied from v4
    assert "bm25_text" in _cols(conn, "unified_index")
    row = conn.execute(
        "SELECT slug, title, snippet, bm25_text FROM unified_index "
        "WHERE slug='s1'").fetchone()
    assert row["title"] == "T"
    assert row["snippet"] == "snip"
    assert row["bm25_text"] is None  # un-migrated row -> NULL (fallback applies)
    conn.close()


def test_reapply_is_noop(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    v1 = db.migrate(conn, MIG)
    v2 = db.migrate(conn, MIG)  # replay
    assert v1 == v2 >= 5
    assert conn.execute("PRAGMA user_version").fetchone()[0] == v2
    conn.close()


def test_replay_tolerant_after_simulated_crash(tmp_path):
    """bm25_text already present but user_version regressed (crash between apply
    and bump) must re-run cleanly (idempotent ADD COLUMN), not 'duplicate column'."""
    conn = db.connect(tmp_path / "m.db")
    terminal = db.migrate(conn, MIG)
    assert terminal >= 5
    conn.execute("PRAGMA user_version=4")  # simulate crash mid-0005
    again = db.migrate(conn, MIG)
    assert again == terminal
    assert conn.execute("PRAGMA user_version").fetchone()[0] == terminal
    conn.close()
