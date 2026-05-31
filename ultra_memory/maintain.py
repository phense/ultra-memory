"""Self-healing maintenance: prune session_events + export views, throttled.

Shared by the async SessionStart hook (via um-hook.cmd), the /memory-maintain
command, and a documented CLI. Pure Python — NO LLM, NO OAuth token (the memory
maintenance slice is prune + export only). Fail-open: a maintenance error must
never block a session.
"""
import datetime
import os

from ultra_memory import memory_lib, retention, memory_export, wiki_sync

# Retention window for session_events (days). Conservative default; rolled into
# sessions.summary before deletion, so nothing is lost — only the raw rows are bounded.
_KEEP_DAYS = 90
# Throttle: skip if the last successful run was within this many hours.
_THROTTLE_HOURS = 20
_META_KEY = "last_maintenance"

# SP-3 Stage 5: the wiki-root injection seam. The expert-wiki roots are NOT known
# to the engine (project-agnostic NFR) — a CONSUMER (Trading) supplies them via
# this env var (os.pathsep- OR comma-separated). UNSET/empty -> wiki_sync is
# skipped entirely, so a pure-memory deployment with no expert-wiki is byte-
# identically unaffected.
_WIKI_ROOTS_ENV = "ULTRA_MEMORY_WIKI_ROOTS"


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


def _resolve_wiki_roots(env):
    """The wiki-root injection seam (project-agnostic). Parse ULTRA_MEMORY_WIKI_ROOTS
    (os.pathsep- OR comma-separated) into a list of Path. UNSET/blank -> [] (the
    pure-memory no-wiki skip). Returns list[Path]."""
    from pathlib import Path
    raw = env.get(_WIKI_ROOTS_ENV, "")
    if not raw or not raw.strip():
        return []
    parts = []
    for chunk in raw.split(os.pathsep):
        parts.extend(chunk.split(","))
    return [Path(p.strip()) for p in parts if p.strip()]


def _maybe_default_embedder(env):
    """Resolve an embedder for wiki_sync's knowledge embeddings: reuse the engine's
    lazy fastembed embedder. Fail-open: if the optional extra is not installed,
    return None (index rows still upsert; embedding is skipped). Never crashes."""
    from ultra_memory import retrieval_core
    try:
        return retrieval_core.default_embedder()
    except Exception:
        return None


def run(conn, *, out_dir, ts=None, keep_days=_KEEP_DAYS, force=False,
        wiki_roots=None, embedder=None, env=None, rebuild_index=False):
    """Throttled prune + export (+ optional wiki_sync). Returns {pruned, exported,
    skipped} — plus a `wiki_sync` summary ONLY when wiki roots are configured.
    Fail-soft: the caller wraps this so an error never blocks a session.

    SP-3 Stage 5: wiki_sync runs INSIDE this same throttle (no second throttle).
    The wiki roots come from `wiki_roots=` (explicit) or, when None, the
    ULTRA_MEMORY_WIKI_ROOTS env seam (`_resolve_wiki_roots`). If there are no roots
    (the pure-memory deployment), wiki_sync is skipped ENTIRELY and the return value
    is byte-identical to pre-Stage-5 behavior."""
    ts = ts or _now_z()
    if env is None:
        env = os.environ
    last = _get_meta(conn, _META_KEY)
    if not force and last:
        try:
            if _hours_between(last, ts) < _THROTTLE_HOURS:
                return {"pruned": 0, "exported": False, "skipped": True}
        except ValueError:
            pass  # unparseable stamp -> proceed (self-heal)
    pruned = retention.prune_session_events(conn, keep_days=keep_days, ts=ts)
    exported = memory_export.export_memory(conn, out_dir, ts=ts)
    result = {"pruned": pruned, "exported": bool(exported), "skipped": False}

    # SP-3 Stage 5 — wiki_sync, inside the throttle, fail-open. Skipped entirely
    # when no roots are configured (pure-memory deployments stay unaffected).
    roots = wiki_roots if wiki_roots is not None else _resolve_wiki_roots(env)
    if roots:
        try:
            emb = embedder if embedder is not None else _maybe_default_embedder(env)
            result["wiki_sync"] = wiki_sync.wiki_sync(
                conn, roots, embedder=emb, rebuild=rebuild_index, ts=ts)
        except Exception as exc:  # fail-open: a sync error never blocks maintenance
            result["wiki_sync"] = {"error": str(exc)}

    _set_meta(conn, _META_KEY, ts)
    return result


def main(argv=None):
    """CLI / hook entry. Resolves DB + out_dir from env; fail-open (exit 0).

    out_dir defaults to <db-dir>/memory_export/views (mirrors the export layout
    consumers already use); override with ULTRA_MEMORY_EXPORT_DIR.

    `--rebuild` (or ULTRA_MEMORY_REBUILD_INDEX=1) forces a one-pass re-population
    of every unified_index row regardless of content_sha256 — the SP-6 #6 (D11)
    bm25_text backfill for rows written by the pre-fix wiki_sync. A rebuild implies
    force (else the throttle would skip the run it was invoked to perform)."""
    import sys
    from pathlib import Path
    args = sys.argv[1:] if argv is None else list(argv)
    rebuild = ("--rebuild" in args) or (
        os.environ.get("ULTRA_MEMORY_REBUILD_INDEX", "") == "1")
    db = os.environ.get("ULTRA_MEMORY_DB", "")
    if not db or not Path(db).is_file():
        return 0
    out_dir = os.environ.get("ULTRA_MEMORY_EXPORT_DIR") or str(
        Path(db).parent / "memory_export" / "views")
    force = rebuild or os.environ.get("ULTRA_MEMORY_MAINTAIN_FORCE", "") == "1"
    try:
        conn = memory_lib.open_memory_db(db)
        try:
            res = run(conn, out_dir=out_dir, force=force, rebuild_index=rebuild)
        finally:
            conn.close()
        sys.stderr.write(f"ultra-memory maintain: {res}\n")
    except Exception as exc:  # never block the session
        sys.stderr.write(f"ultra-memory maintain skipped: {exc}\n")
    return 0
