"""SP-3 Stage 5 — `wiki_sync`: the Tier-1 wiki->memory mirror (§5.4, D8).

POPULATION ONLY (retrieval is Stage 6). `wiki_sync` walks each Expert-Knowledge
root, upserts every `<root>/<topic>/**/*.md` page into the `unified_index` table
beside the memory tables, reconciles orphans, and embeds each page's `title+
snippet` into the SHARED `embeddings` table (`target_kind='knowledge'`), reusing
`retrieval_core.get_or_embed_batch`'s `content_sha256` cache invalidation. No new
embedder, no model download.

PROJECT-AGNOSTIC (the central NFR of this stage): `wiki_sync(conn, wiki_roots, …)`
takes `wiki_roots` as a CONSUMER-FED iterable of root paths — exactly like Stage
3's `mirror_cross_store_links(conn, wiki_edges, …)`. The engine imports NOTHING
out of Trading (no topic-model module, no topics-registry parser, not even
PyYAML). Concretely:
  - `topic` is derived GENERICALLY from the path: the FIRST path component under
    the root (`<root>/trading/concepts/x.md` -> `trading`).
  - `page_type` (frontmatter `type:`), `title` (frontmatter `title:`, else the
    first `# heading`, else the slug), and a rendered `snippet` (frontmatter
    stripped) are parsed with GENERIC markdown parsing — a tiny hand-rolled YAML
    front-matter scanner, NOT PyYAML.
  - `slug` = the file stem.

Pure-Python, NO LLM, fail-open: a missing root dir, an unreadable / parse-failing
single page, or an embed failure must NOT crash — it is logged into the summary's
`errors` count and the sync continues. Idempotent: a 2nd sync is a near no-op
(content_sha256 match -> skip + no re-embed).

Returns a small summary {upserted, skipped, pruned, embedded, errors} so
`maintain.run` can surface the reconciliation count (Risk §14.4).
"""
import json

from ultra_memory import retrieval_core
from ultra_memory.redact_secrets import strip_secrets


def _split_frontmatter(text):
    """Generic markdown front-matter split. Returns (frontmatter_dict, body).

    Scans a leading `---` ... `---` YAML-ish block with a flat `key: value` shape
    (sufficient for wiki frontmatter; we do NOT parse nested YAML — that would pull
    in PyYAML and is unnecessary for the fields this mirror reads). If there is no
    front-matter block, returns ({}, text).
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    # First line is the opening '---'; find the closing fence.
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    fm = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip("'\"")
        if key:
            fm[key] = val
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return fm, body


def _first_heading(body):
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return None


def _snippet(body, *, limit=400):
    """A rendered snippet: front-matter already stripped; collapse blank runs and
    cap length. Generic — no wiki-specific rendering. This is the DISPLAY preview
    (recall UX), NOT the BM25 document — those have different right answers (a
    400-char preview is correct UX; the full body is correct for ranking). See
    `_bm25_text`."""
    text = " ".join(body.split())
    return text[:limit]


def _bm25_text(body):
    """The FULL collapsed body for the knowledge-side BM25 document (SP-6 #6, D11):
    whitespace-collapsed, NO length cap. `unified_query._knowledge_doc_text` indexes
    this so a query term in a page's back half ranks — matching `wiki_query`'s
    full-text BM25 (closes the SP-5 parity tail-divergence). Generic, no wiki-
    specific rendering. Stored in `unified_index.bm25_text` (migration 0005)."""
    return " ".join(body.split())


def _iter_pages(root):
    """Yield (path, topic, slug) for every `<root>/<topic>/**/*.md` page.

    `topic` is the FIRST path component under the root (generic path derivation).
    Pages that sit directly at the root (no topic dir) are skipped — every wiki
    page lives under a topic (SP-2 invariant). Fail-open on an unreadable tree.
    """
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.md")):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) < 2:
            # Directly under the root (e.g. index.md / SCHEMA.md) — not a topic page.
            continue
        topic = parts[0]
        yield path, topic, path.stem


def wiki_sync(conn, wiki_roots, *, embedder=None, rebuild=False, ts):
    """Mirror Expert-Knowledge pages into `unified_index` + the shared embeddings
    cache. Idempotent, reconciling, fail-open. POPULATION ONLY (§5.4, D8).

    `wiki_roots` is a consumer-fed iterable of root paths (str or Path). `embedder`
    is list[str] -> list[list[float]] (None -> skip embedding, still upsert rows).
    Returns {upserted, skipped, pruned, embedded, errors}.

    `rebuild=True` forces every page to re-populate even when its `content_sha256`
    is unchanged — the one-pass backfill for SP-6 #6 (D11): a row written by the
    PRE-fix wiki_sync carries a NULL `bm25_text` that the normal sha-skip would
    leave forever. A `--rebuild` run repopulates `bm25_text` (and re-embeds) for
    all pages. Single-root/normal callers leave it False (sha-skip preserved).
    """
    from pathlib import Path

    roots = [Path(r) for r in wiki_roots]
    summary = {"upserted": 0, "skipped": 0, "pruned": 0, "embedded": 0, "errors": 0}

    # Slugs seen in THIS call's roots — the reconciliation prune is scoped to them
    # (a row from a root not in this call must survive). We track the (slug -> path)
    # so we can also detect within-root orphans precisely.
    seen_slugs = set()
    embed_targets = []  # (slug, title+snippet) for pages that changed / are new

    for root in roots:
        for path, topic, slug in _iter_pages(root):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                summary["errors"] += 1
                continue
            seen_slugs.add(slug)
            try:
                fm, body = _split_frontmatter(text)
                page_type = fm.get("type")
                title = fm.get("title") or _first_heading(body) or slug
                snippet = _snippet(body)
                bm25_text = _bm25_text(body)
                frontmatter_json = json.dumps(fm, ensure_ascii=False, sort_keys=True)
                sha = retrieval_core.content_sha256(text)
                # wiki_sync is a REDACTION CHOKEPOINT, equivalent to the memory
                # write path (save_memory). The documented free-form `Edit`
                # exception can land an unredacted secret on a wiki page; redact
                # the queryable text before it reaches the unified_index mirror
                # (which unified_recall + the rehydrate gist read). `content_sha256`
                # is computed on the RAW page text above (so the idempotency/cache
                # key is stable + matches the on-disk file), then we redact.
                title = strip_secrets(title)
                snippet = strip_secrets(snippet)
                bm25_text = strip_secrets(bm25_text)
                frontmatter_json = strip_secrets(frontmatter_json)
            except Exception:
                summary["errors"] += 1
                continue

            prior = conn.execute(
                "SELECT content_sha256 FROM unified_index WHERE slug=?",
                (slug,)).fetchone()
            if (not rebuild and prior is not None
                    and prior["content_sha256"] == sha):
                summary["skipped"] += 1
                continue

            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO unified_index "
                    "(slug, topic, page_type, title, snippet, bm25_text, "
                    " frontmatter, path, content_sha256, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(slug) DO UPDATE SET "
                    "topic=excluded.topic, page_type=excluded.page_type, "
                    "title=excluded.title, snippet=excluded.snippet, "
                    "bm25_text=excluded.bm25_text, "
                    "frontmatter=excluded.frontmatter, path=excluded.path, "
                    "content_sha256=excluded.content_sha256, "
                    "updated_at=excluded.updated_at",
                    (slug, topic, page_type, title, snippet, bm25_text,
                     frontmatter_json, str(path), sha, ts),
                )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                summary["errors"] += 1
                continue
            summary["upserted"] += 1
            # Only (re-)embed pages that changed/are new; the embeddings cache's own
            # sha invalidation is a second guard, but we avoid even feeding skips.
            embed_targets.append((slug, f"{title}\n{snippet}"))

    # Reconciliation: prune unified_index rows whose slug is no longer a file
    # WITHIN the synced roots. Scope by the PATH-PREFIX of this call's roots — a row
    # whose `path` sits under a synced root but whose slug was NOT seen this sync is
    # an orphan (mirrors memory_export's path-based orphan prune). A row from an
    # un-synced root is never touched (cross-root safety). R4 FIX 1: the prior code
    # recomputed `synced_topics` from the CURRENT on-disk pages and gated on
    # `topic IN (...)`; when the deleted page was the LAST page of its topic, that
    # topic dropped out and its orphan row (a phantom pointing at a deleted file)
    # survived forever. Scoping to the root path-prefix removes that dependence on a
    # topic still having surviving files.
    root_prefixes = [str(root) + "/" for root in roots]
    if root_prefixes:
        # `path LIKE <prefix>%` per synced root (the prefix already ends in os.sep,
        # so a sibling root that merely shares a name-prefix is not matched). LIKE
        # special chars (% _) do not occur in a normal filesystem path; if a root
        # contained one it would only WIDEN the candidate set, then the seen_slugs +
        # path-prefix membership check below still prunes correctly within roots.
        like_clauses = " OR ".join("path LIKE ?" for _ in root_prefixes)
        existing = conn.execute(
            f"SELECT slug, path FROM unified_index WHERE {like_clauses}",
            tuple(p + "%" for p in root_prefixes)).fetchall()
        orphans = [
            r["slug"] for r in existing
            if r["slug"] not in seen_slugs
            and r["path"] is not None
            and any(r["path"].startswith(p) for p in root_prefixes)]
        if orphans:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(
                    "DELETE FROM unified_index WHERE slug=?",
                    [(s,) for s in orphans])
                # Prune the orphaned knowledge embedding too (wiki_sync owns the
                # knowledge-kind vectors it writes): a surviving phantom vector would
                # let the embed backend rank a deleted page (the cosine half of the
                # phantom). Fail-open within the same txn.
                conn.executemany(
                    "DELETE FROM embeddings WHERE target_kind='knowledge' "
                    "AND target_id=?",
                    [(s,) for s in orphans])
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                summary["errors"] += 1
            else:
                summary["pruned"] += len(orphans)

    # Embed changed/new pages into the SHARED embeddings table as 'knowledge'.
    # Reuses get_or_embed_batch + its content_sha256 invalidation. Fail-open: an
    # embed failure must not undo the index upserts (Risk §14.4) — count + continue.
    if embedder is not None and embed_targets:
        items = [("knowledge", slug, text) for slug, text in embed_targets]
        try:
            result = retrieval_core.get_or_embed_batch(
                conn, items, embedder=embedder)
            summary["embedded"] = len(result)
        except Exception:
            summary["errors"] += 1

    return summary


def main(argv=None):
    """CLI: populate the unified_index + knowledge-embeddings mirror from wiki roots.

    `python -m ultra_memory.wiki_sync [--roots R] [--db DB] [--rebuild] [--no-embed]`
    Roots default to $ULTRA_MEMORY_WIKI_ROOTS, db to $ULTRA_MEMORY_DB (then
    ~/.ultra-memory/memory.db). No roots → rc 0 no-op (pure-memory skip). Opens +
    migrates the db itself (open_memory_db). Fail-soft: an embedder-build failure
    degrades to a BM25-only populate; rc 1 only on a hard open/populate failure."""
    import argparse
    import os
    import sys
    import time
    from pathlib import Path

    ap = argparse.ArgumentParser(
        prog="ultra_memory.wiki_sync",
        description="Populate the unified_index + knowledge-embeddings mirror.")
    ap.add_argument("--roots", default=os.environ.get("ULTRA_MEMORY_WIKI_ROOTS", ""),
                    help="os.pathsep- or comma-separated wiki root paths "
                         "(default: $ULTRA_MEMORY_WIKI_ROOTS)")
    ap.add_argument("--db", default=(os.environ.get("ULTRA_MEMORY_DB")
                                     or str(Path.home() / ".ultra-memory" / "memory.db")))
    ap.add_argument("--rebuild", action="store_true",
                    help="force re-populate every page even when content is unchanged")
    ap.add_argument("--no-embed", action="store_true",
                    help="skip embedding (BM25-only populate; no fastembed load)")
    args = ap.parse_args(argv)

    roots = []
    for chunk in (args.roots or "").split(os.pathsep):
        roots.extend(c.strip() for c in chunk.split(",") if c.strip())
    if not roots:
        print("wiki_sync: no roots (set --roots or ULTRA_MEMORY_WIKI_ROOTS); "
              "nothing to do", file=sys.stderr)
        return 0

    from ultra_memory import memory_lib
    try:
        conn = memory_lib.open_memory_db(args.db)
    except Exception as exc:  # hard failure: cannot open/migrate the store
        print(f"wiki_sync: cannot open db {args.db}: {exc}", file=sys.stderr)
        return 1

    embedder = None
    if not args.no_embed:
        try:
            embedder = retrieval_core.default_embedder()
        except Exception as exc:  # fail-soft: BM25-only populate
            print(f"wiki_sync: no embedder ({type(exc).__name__}); BM25-only populate",
                  file=sys.stderr)

    try:
        summary = wiki_sync(conn, roots, embedder=embedder,
                            rebuild=args.rebuild, ts=int(time.time()))
        conn.commit()
    except Exception as exc:  # hard failure: populate aborted
        print(f"wiki_sync: populate failed: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass

    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
