"""/memory-setup support: the idempotency-critical bits, unit-tested.

The slash command does venv bootstrap (uv sync) + the optional legacy import in
shell; the decisions that must be deterministic + tested live here. Production
code (NOT just tests) stamps meta.import_complete — without it db_ready() is
False forever and the session hooks never activate (the §2 trap).
"""
import shutil

from ultra_memory import memory_lib

# External tools the plugin requires on PATH to function. `/memory-setup` checks
# these in a preflight and refuses to proceed if any is missing.
#   - uv:  provisions the Python 3.13 runtime venv + the optional retrieval/mcp
#          extras (the engine itself is pure Python 3.13 + SQLite — no other
#          binary is shelled).
#   - git: the rollback/safety model is git-backed. The deterministic export
#          (memory.dump.sql + VACUUM snapshot + markdown views) is "the sole
#          git-committed rollback artifact" (memory_export §7.1), and the
#          wiki/maintenance lifecycle is archive-never-delete *via git*. The
#          engine never shells git directly; the REQUIREMENT is on the rollback
#          model, not a runtime call — but without git there is no restore net,
#          so it is a hard prerequisite, not advisory.
REQUIRED_TOOLS = ("uv", "git")


def check_prerequisites(which=shutil.which):
    """Map each required external tool → bool(present on PATH). `which` is
    injectable (shutil.which by default) so tests need no real binaries. Pure —
    no side effects."""
    return {name: bool(which(name)) for name in REQUIRED_TOOLS}


def missing_prerequisites(which=shutil.which):
    """The REQUIRED_TOOLS not found on PATH, in REQUIRED_TOOLS order. Empty list
    => all present. The /memory-setup preflight aborts with a clear message when
    this is non-empty."""
    present = check_prerequisites(which=which)
    return [name for name in REQUIRED_TOOLS if not present[name]]


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
