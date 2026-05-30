"""Deterministic export = the git rollback artifact (spec §7.1).

Writes memory.dump.sql + a VACUUM INTO snapshot + regenerated markdown views.
Skip-if-unchanged on a content hash that EXCLUDES access telemetry, so
reinforcement churn never drives a commit. Never git-adds the live .db.
"""
import hashlib
from pathlib import Path

_STABLE_COLS = ("id", "type", "title", "description", "index_hook", "node_type",
                "body", "status", "supersedes", "origin_session_id")


def _content_hash(conn):
    h = hashlib.sha256()
    cols = ", ".join(_STABLE_COLS)
    for row in conn.execute(f"SELECT {cols} FROM memories ORDER BY id"):
        h.update(repr(tuple(row)).encode("utf-8"))
    for row in conn.execute(
            "SELECT event_key, kind, title, detail FROM session_events "
            "ORDER BY event_key"):
        h.update(repr(tuple(row)).encode("utf-8"))
    return h.hexdigest()


def _frontmatter(row):
    return (
        "---\n"
        f"name: {row['id']}\n"
        f"description: {row['description'] or ''}\n"
        "metadata:\n"
        f"  node_type: {row['node_type'] or 'memory'}\n"
        f"  type: {row['type']}\n"
        f"  originSessionId: {row['origin_session_id'] or ''}\n"
        "---\n\n"
    )


def export_memory(conn, out_dir, *, ts, snapshot=True):
    """Write the consistent dump + snapshot + views. Returns True if written,
    False if skipped (content unchanged)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    hash_path = out_dir / "content.hash"
    new_hash = _content_hash(conn)
    if hash_path.exists() and hash_path.read_text().strip() == new_hash:
        return False

    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    (out_dir / "memory.dump.sql").write_text("\n".join(conn.iterdump()))

    if snapshot:
        snap = out_dir / "memory.snapshot.db"
        if snap.exists():
            snap.unlink()
        # VACUUM INTO does not accept a bound parameter in sqlite3 — use an
        # escaped string literal (tmp paths are quote-free, but be safe).
        safe = str(snap).replace("'", "''")
        conn.execute(f"VACUUM INTO '{safe}'")

    views = out_dir / "views"
    views.mkdir(exist_ok=True)
    index_lines = []
    rows = conn.execute(
        "SELECT * FROM memories WHERE status='active' ORDER BY id").fetchall()
    for row in rows:
        (views / f"{row['id']}.md").write_text(_frontmatter(row) + (row["body"] or ""))
        hook = f" — {row['index_hook']}" if row["index_hook"] else ""
        index_lines.append(f"- [{row['title']}]({row['id']}.md){hook}")
    (views / "MEMORY.md").write_text("\n".join(index_lines) + "\n")

    hash_path.write_text(new_hash)
    return True
