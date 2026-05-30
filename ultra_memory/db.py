"""memory.db connection discipline + forward-only migration runner.

Connections: WAL, busy_timeout=30000ms, autocommit (isolation_level=None) with
explicit BEGIN IMMEDIATE/COMMIT around writes (spec §6 single-writer discipline).
Migrations: ordered .sql files named NNNN_name.sql, applied when their version
exceeds PRAGMA user_version. Each migration's statements AND its version-bump run
inside one explicit transaction (SQLite DDL + PRAGMA user_version are both
transactional), so a crash partway rolls the whole migration back — version and
schema never desync. ADD COLUMN re-application is tolerated (idempotent), so a
restore/replay against an already-shaped DB cannot wedge the runner.
"""
import sqlite3
from pathlib import Path


def _split_statements(sql):
    """Split a simple DDL migration into individual statements. Drops blank lines
    and full-line `--` comments, then splits on `;`. Our migrations contain no
    semicolons inside string literals, so this is sufficient and keeps each
    statement runnable inside one explicit transaction (executescript would
    auto-commit and break atomicity)."""
    kept = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(kept).split(";") if s.strip()]


def _apply_statement(conn, stmt):
    """Run one migration statement, tolerating a re-applied ADD COLUMN. SQLite has
    no `ADD COLUMN IF NOT EXISTS`; a duplicate-column error on replay means the
    column is already present, so treat it as already-applied."""
    try:
        conn.execute(stmt)
    except sqlite3.OperationalError as exc:
        if "duplicate column name" in str(exc).lower():
            return
        raise


def connect(db_path, *, busy_timeout_ms=30000):
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn, migrations_dir):
    """Apply all migrations whose version > user_version. Returns the new version."""
    migrations_dir = Path(migrations_dir)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    applied = current
    for path in sorted(migrations_dir.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version <= current:
            continue
        statements = _split_statements(path.read_text(encoding="utf-8"))
        conn.execute("BEGIN")
        try:
            for stmt in statements:
                _apply_statement(conn, stmt)
            conn.execute(f"PRAGMA user_version={version}")
            # Mirror into meta.schema_version (§7.3): PRAGMA user_version is NOT
            # serialised by iterdump, but meta rows are — so the committed dump and
            # the bootstrap state machine get a queryable version. In the same txn,
            # so the mirror can never drift from user_version. Guarded on the meta
            # table existing (it may not, in a migration set that doesn't define it).
            has_meta = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            if has_meta:
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(version),),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied = version
    return applied
