"""/memory-setup support: the idempotency-critical bits, unit-tested.

The slash command does venv bootstrap (uv sync) + the optional legacy import in
shell; the decisions that must be deterministic + tested live here. Production
code (NOT just tests) stamps meta.import_complete — without it db_ready() is
False forever and the session hooks never activate (the §2 trap).
"""
from ultra_memory import memory_lib


def _import_complete(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='import_complete'").fetchone()
    return bool(row) and str(row[0]) == "1"


def mark_import_complete(db_path):
    """Stamp meta.import_complete='1'. Returns True if newly stamped, False if
    it was already set (idempotent)."""
    conn = memory_lib.open_memory_db(str(db_path))
    try:
        if _import_complete(conn):
            return False
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('import_complete', '1') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return True
    finally:
        conn.close()


def should_import_legacy(db_path):
    """True only when the one-time legacy import has not yet run (import_complete
    unset). Greenfield consumers with no legacy dir simply stamp directly."""
    conn = memory_lib.open_memory_db(str(db_path))
    try:
        return not _import_complete(conn)
    finally:
        conn.close()
