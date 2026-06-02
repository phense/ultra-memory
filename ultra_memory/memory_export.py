"""Deterministic export = the git rollback artifact (spec §7.1).

Writes memory.dump.sql + a VACUUM INTO snapshot + regenerated markdown views.
Skip-if-unchanged on a content hash that EXCLUDES access telemetry, so
reinforcement churn never drives a commit. Never git-adds the live .db.
"""
import datetime
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

    # Snapshot FIRST: a failure here must not touch the prior dump/views — OR the
    # prior snapshot. VACUUM INTO a tmp path then os.replace (mirroring the dump swap
    # below): VACUUM INTO refuses to overwrite an existing file (hence the tmp must be
    # removed first), but writing the tmp and swapping atomically means a VACUUM
    # failure AFTER the unlink (disk-full / I/O error / SIGTERM) leaves the PRIOR good
    # snapshot intact instead of destroying it (R3 bughunt FIX 2).
    if snapshot:
        snap = out_dir / "memory.snapshot.db"
        tmp_snap = out_dir / "memory.snapshot.db.tmp"
        tmp_snap.unlink(missing_ok=True)  # VACUUM INTO won't overwrite a stale tmp
        # VACUUM INTO does not accept a bound parameter in sqlite3 — escaped literal.
        safe = str(tmp_snap).replace("'", "''")
        try:
            conn.execute(f"VACUUM INTO '{safe}'")
            os.replace(tmp_snap, snap)
        except Exception:
            # Never leave a torn temp snapshot behind on any failure (VACUUM or the
            # swap); the prior memory.snapshot.db is untouched.
            tmp_snap.unlink(missing_ok=True)
            raise

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
    # Atomic write (R3 bughunt FIX 3): the projection is GIT-TRACKED and Stage 3
    # commits whatever is on disk. Path.write_text truncates-then-writes, so a
    # SIGKILL/crash mid-write would leave a torn projection to be committed. Write to
    # a sibling `.tmp` then os.replace into place — all-or-nothing under SIGKILL,
    # mirroring the export dump's atomic swap.
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, out)
    return len(rows)


# ---------------------------------------------------------------------------
# Model B (projection-coupled skill evolution, spec 2026-06-02) — the UNION-BLEND
# managed block. Distinct from export_learnings_projection above (which projects ALL
# active rows of ONE index_hook, chronologically, into a standalone Learnings.md):
# this renders the top-N learning lessons across a UNION of index_hooks, ranked by a
# recency-decayed outcome weight, for the <!-- BEGIN/END auto-learnings --> region of
# a generated SKILL.md. Locked params: feed = UNION of (source_domain, gen-<slug>)
# de-duped; node_type='learning'; cap = 20; ranking = blend
# score = outcome_weight * 0.5 ** (age_days / HALFLIFE_DAYS).
#
# Time-dependent BY DESIGN (a ranked view that decays) — so it takes an explicit
# `now` (never an ambient clock): deterministic for a fixed `now`, testable, and the
# beat threads the run timestamp. Zero LLM, no network — a pure store projection.
# ---------------------------------------------------------------------------

BLEND_HALFLIFE_DAYS = 45        # spec: recency decay half-life (default; tunable)
BLEND_CAP = 20                  # spec: max lessons in the managed block
_EMPTY_BLOCK = "_No learnings recorded yet._\n"


def _parse_dt(value):
    """Parse an ISO timestamp (with or without a 'Z'/offset, or date-only) to a
    naive datetime (all treated as UTC for the diff). Fail-open: an unparseable /
    empty value → None (the caller degrades to age 0 = no decay)."""
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).strip())
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=None)


def _age_days(created_at, now):
    c = _parse_dt(created_at)
    n = _parse_dt(now)
    if c is None or n is None:
        return 0.0
    # A future created_at (clock skew) clamps to 0 so decay never exceeds 1.0.
    return max(0.0, (n - c).total_seconds() / 86400.0)


def _recency_decay(age_days, halflife_days):
    if halflife_days <= 0:
        return 1.0
    return 0.5 ** (age_days / halflife_days)


def render_union_blend_block(conn, *, hooks, now, cap=BLEND_CAP,
                             halflife_days=BLEND_HALFLIFE_DAYS):
    """Render the union-blend managed block (markdown, NO frontmatter, NO markers —
    skill_fs owns the markers). `hooks` is the union of index_hooks to draw from
    (e.g. [source_domain, 'gen-<slug>']); de-duped, falsy entries dropped. Selects
    active `node_type='learning'` rows in those hooks, ranks by
    outcome_weight * recency_decay(age vs `now`) descending (ties → most-recent
    first, then id desc for stability), keeps the top `cap`, and renders each as a
    `### {title}` entry with its body verbatim. An empty feed → the sentinel."""
    seen = []
    for h in hooks:
        if h and h not in seen:
            seen.append(h)
    if not seen:
        return _EMPTY_BLOCK
    placeholders = ",".join("?" * len(seen))
    rows = conn.execute(
        f"SELECT id, title, body, outcome_weight, created_at FROM memories "
        f"WHERE status='active' AND node_type='learning' "
        f"AND index_hook IN ({placeholders})",
        tuple(seen),
    ).fetchall()
    by_id = {r["id"]: r for r in rows}        # de-dup defensively across hooks
    scored = []
    for r in by_id.values():
        w = r["outcome_weight"] if r["outcome_weight"] is not None else 1.0
        decay = _recency_decay(_age_days(r["created_at"], now), halflife_days)
        scored.append((float(w) * decay, r))
    # One compound-key sort: score desc, ties → most-recent first, then id desc.
    # All three keys descend together, so a single sort is equivalent to the prior
    # stable two-pass (recency/id desc, then score desc) — and half the work.
    scored.sort(key=lambda t: (t[0], t[1]["created_at"] or "", t[1]["id"]), reverse=True)
    top = scored[: max(0, cap)]
    if not top:
        return _EMPTY_BLOCK
    return "\n".join(_learning_entry(t[1]) for t in top)
