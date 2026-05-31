"""Self-healing maintenance: prune session_events + export views, throttled.

Shared by the async SessionStart hook (via um-hook.cmd), the /memory-maintain
command, and a documented CLI. Pure Python — NO LLM, NO OAuth token (the memory
maintenance slice is prune + export only). Fail-open: a maintenance error must
never block a session.
"""
import datetime
import os

from ultra_memory import memory_lib, retention, memory_export

# Retention window for session_events (days). Conservative default; rolled into
# sessions.summary before deletion, so nothing is lost — only the raw rows are bounded.
_KEEP_DAYS = 90
# Throttle: skip if the last successful run was within this many hours.
_THROTTLE_HOURS = 20
_META_KEY = "last_maintenance"


def _now_z():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn, key, value):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _hours_between(earlier_z, later_z):
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    a = datetime.datetime.strptime(earlier_z, fmt)
    b = datetime.datetime.strptime(later_z, fmt)
    return (b - a).total_seconds() / 3600.0


def run(conn, *, out_dir, ts=None, keep_days=_KEEP_DAYS, force=False):
    """Throttled prune + export. Returns {pruned, exported, skipped}. Fail-soft:
    the caller wraps this so an error never blocks a session."""
    ts = ts or _now_z()
    last = _get_meta(conn, _META_KEY)
    if not force and last:
        try:
            if _hours_between(last, ts) < _THROTTLE_HOURS:
                return {"pruned": 0, "exported": False, "skipped": True}
        except ValueError:
            pass  # unparseable stamp -> proceed (self-heal)
    pruned = retention.prune_session_events(conn, keep_days=keep_days, ts=ts)
    exported = memory_export.export_memory(conn, out_dir, ts=ts)
    _set_meta(conn, _META_KEY, ts)
    return {"pruned": pruned, "exported": bool(exported), "skipped": False}


def main(argv=None):
    """CLI / hook entry. Resolves DB + out_dir from env; fail-open (exit 0).

    out_dir defaults to <db-dir>/memory_export/views (mirrors the export layout
    consumers already use); override with ULTRA_MEMORY_EXPORT_DIR.
    """
    import sys
    from pathlib import Path
    db = os.environ.get("ULTRA_MEMORY_DB", "")
    if not db or not Path(db).is_file():
        return 0
    out_dir = os.environ.get("ULTRA_MEMORY_EXPORT_DIR") or str(
        Path(db).parent / "memory_export" / "views")
    force = os.environ.get("ULTRA_MEMORY_MAINTAIN_FORCE", "") == "1"
    try:
        conn = memory_lib.open_memory_db(db)
        try:
            res = run(conn, out_dir=out_dir, force=force)
        finally:
            conn.close()
        sys.stderr.write(f"ultra-memory maintain: {res}\n")
    except Exception as exc:  # never block the session
        sys.stderr.write(f"ultra-memory maintain skipped: {exc}\n")
    return 0
