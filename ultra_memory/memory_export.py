"""Deterministic export = the git rollback artifact (spec §7.1).

Writes memory.dump.sql + a VACUUM INTO snapshot + regenerated markdown views.
Skip-if-unchanged on a content hash that EXCLUDES access telemetry, so
reinforcement churn never drives a commit. Never git-adds the live .db.
"""
import hashlib
import os
from pathlib import Path

from .redact_secrets import strip_secrets

# `outcome_weight` is included (SP-7/SP-8): a set_outcome_weight write is
# SEMANTICALLY meaningful — it changes recall ranking — so a weight change MUST
# drive a re-export, or the git-committed rollback dump goes stale and a restore
# reverts the weight. Pure access telemetry (access_count/last_accessed/
# last_verified) stays EXCLUDED — reinforcement churn must not drive a commit.
_STABLE_COLS = ("id", "type", "title", "description", "index_hook", "node_type",
                "file_slug", "sort_order", "body", "status", "supersedes",
                "origin_session_id", "topic", "created_by", "outcome_weight")


def _content_hash(conn):
    h = hashlib.sha256()
    cols = ", ".join(_STABLE_COLS)
    for row in conn.execute(f"SELECT {cols} FROM memories ORDER BY id"):
        h.update(repr(tuple(row)).encode("utf-8"))
    # `outcome_signal` is the session_event's semantically-meaningful attribution
    # payload (the EWMA fold's evidence) — include it so a signal change re-exports.
    for row in conn.execute(
            "SELECT event_key, kind, title, detail, outcome_signal "
            "FROM session_events ORDER BY event_key"):
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
    False if skipped (content unchanged).

    Robustness (L5/L6): hash + dump + view contents are read inside ONE explicit
    read transaction (consistent snapshot); the VACUUM snapshot runs first, then
    views, then the dump is swapped into place atomically (tmp → os.replace), and
    the content hash is written LAST. So a failure partway never corrupts the prior
    good dump, and an interrupted run re-runs (no stale hash) instead of skipping."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    hash_path = out_dir / "content.hash"

    # Consistent read snapshot for hash + dump + view rows. wal_checkpoint and
    # VACUUM cannot run inside a transaction, so they sit outside this block.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("BEGIN")
    try:
        new_hash = _content_hash(conn)
        if hash_path.exists() and hash_path.read_text().strip() == new_hash:
            return False
        # iterdump() does NOT serialize PRAGMA user_version; append it so the dump —
        # the sole git-committed rollback artifact — round-trips the schema version.
        # Redact the whole dump (§7.5): columns like links.evidence / meta.value /
        # sessions.summary aren't on the write-path chokepoint; [REDACTED] inside a
        # SQL string literal stays valid.
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
        dump = strip_secrets("\n".join(conn.iterdump()))
        dump += f"\nPRAGMA user_version={schema_version};\n"
        # Preserve the harness FILENAME (file_slug, underscore) and the curated
        # MEMORY.md order (sort_order) — the hyphenated DB id must not drive the
        # filename/link or a roundtrip renames files. NULL sort_order sorts last.
        rows = conn.execute(
            "SELECT * FROM memories WHERE status='active' "
            "ORDER BY sort_order IS NULL, sort_order, id").fetchall()
    finally:
        conn.execute("COMMIT")

    # Build view contents from the snapshot rows (no writes yet).
    view_files = {}
    index_lines = []
    for row in rows:
        slug = row["file_slug"] or row["id"]
        view_files[f"{slug}.md"] = _frontmatter(row) + (row["body"] or "")
        hook = f" — {row['index_hook']}" if row["index_hook"] else ""
        index_lines.append(f"- [{row['title']}]({slug}.md){hook}")
    view_files["MEMORY.md"] = "\n".join(index_lines) + "\n"

    # Snapshot FIRST: a failure here must not touch the prior dump/views.
    if snapshot:
        snap = out_dir / "memory.snapshot.db"
        if snap.exists():
            snap.unlink()
        # VACUUM INTO does not accept a bound parameter in sqlite3 — escaped literal.
        safe = str(snap).replace("'", "''")
        conn.execute(f"VACUUM INTO '{safe}'")

    views = out_dir / "views"
    views.mkdir(parents=True, exist_ok=True)
    # Prune orphan views: a deleted (status!='active') or renamed memory must not
    # leave a phantom .md in the git-tracked views/ (it is absent from view_files).
    keep = set(view_files)
    for stale in views.glob("*.md"):
        if stale.name not in keep:
            stale.unlink()
    for name, content in view_files.items():
        (views / name).write_text(content)

    # Swap the dump into place atomically — the rollback artifact is never torn,
    # and the prior dump survives any failure before this point.
    tmp = out_dir / "memory.dump.sql.tmp"
    tmp.write_text(dump)
    os.replace(tmp, out_dir / "memory.dump.sql")

    # Hash LAST → an interrupted export re-runs rather than skipping. But first
    # re-validate against the live DB: if a writer landed between the read snapshot
    # and now, the artifacts (dump/views from the old snapshot vs snapshot.db from a
    # later VACUUM) may disagree — so withhold the hash and let the next run
    # re-export a mutually-consistent set instead of skipping with a stale snapshot.
    conn.execute("BEGIN")
    try:
        live_hash = _content_hash(conn)
    finally:
        conn.execute("COMMIT")
    if live_hash == new_hash:
        hash_path.write_text(new_hash)
    return True


# ---------------------------------------------------------------------------
# Generic Learnings projection (SP-3 Stage 7a, D14/D15 — the §7a SUBSTRATE's
# PROJECTION capability, NOT the loop). The DB is the system of record for
# graduated learnings (rows in `memories`); the per-skill Learnings.md surface
# becomes a regenerated, git-trackable PROJECTION — exactly the way export_memory
# regenerates the views/ above. The §7a loop (SP-6/SP-7) is NOT built here; this
# only materializes the projection the loop will later feed.
#
# PROJECT-AGNOSTIC (hard NFR): the output `path` and the `skill_tag` are
# CONSUMER-fed parameters. There is NO Trading literal, no hardcoded skill name,
# no wiki dependency — an arbitrary consumer tag projects to an arbitrary consumer
# path. Deterministic: a stable ORDER BY → re-run is byte-identical.
# ---------------------------------------------------------------------------

def _learning_entry(row):
    """One projected learning block. Body verbatim (already redacted at write)."""
    body = (row["body"] or "").strip()
    return f"### {row['title']}\n\n{body}\n"


def export_learnings_projection(conn, path, *, skill_tag, title=None):
    """Regenerate a Learnings-style markdown projection from the store, filtered to
    the active memories carrying `skill_tag` (matched against `index_hook`). Writes
    `path` (parents created). Returns the count of projected learnings.

    AGNOSTIC: both the output `path` and the `skill_tag` are consumer-supplied —
    the engine names no skill and no path of its own. DETERMINISTIC: rows are
    selected `WHERE status='active' AND index_hook=?` and ordered by a stable key
    (created_at, id) so a re-run with an unchanged store is byte-identical (the §7a
    projection guarantee, mirroring the export views). No LLM, no network.

    This is the read-only PROJECTION (D14) — the canonical store is the DB; this
    surface is rebuildable and never the write path. Inactive (deleted/redirect)
    learnings are excluded, like the export views' active filter.
    """
    rows = conn.execute(
        "SELECT id, title, body, index_hook FROM memories "
        "WHERE status='active' AND index_hook=? "
        "ORDER BY created_at, id",
        (skill_tag,),
    ).fetchall()

    heading = title or f"# Learnings — {skill_tag}"
    parts = [heading, ""]
    if rows:
        parts.extend(_learning_entry(r) for r in rows)
    else:
        parts.append("_No learnings recorded yet._\n")
    content = "\n".join(parts)

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content)
    return len(rows)
