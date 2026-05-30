"""memory.db connection discipline + forward-only migration runner.

Connections: WAL, busy_timeout=30000ms, autocommit (isolation_level=None) with
explicit BEGIN IMMEDIATE/COMMIT around writes (spec §6 single-writer discipline).
Migrations: ordered .sql files named NNNN_name.sql, applied when their version
exceeds PRAGMA user_version. Migrations MUST be idempotent (CREATE ... IF NOT EXISTS)
so a crash between apply and version-bump is safe to re-run.
"""
import sqlite3
from pathlib import Path


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
        if version > current:
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute(f"PRAGMA user_version={version}")
            applied = version
    return applied
