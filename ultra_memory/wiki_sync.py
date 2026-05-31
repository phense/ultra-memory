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
    cap length. Generic — no wiki-specific rendering."""
    text = " ".join(body.split())
    return text[:limit]


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


def wiki_sync(conn, wiki_roots, *, embedder=None, ts):
    """Mirror Expert-Knowledge pages into `unified_index` + the shared embeddings
    cache. Idempotent, reconciling, fail-open. POPULATION ONLY (§5.4, D8).

    `wiki_roots` is a consumer-fed iterable of root paths (str or Path). `embedder`
    is list[str] -> list[list[float]] (None -> skip embedding, still upsert rows).
    Returns {upserted, skipped, pruned, embedded, errors}.
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
                frontmatter_json = json.dumps(fm, ensure_ascii=False, sort_keys=True)
                sha = retrieval_core.content_sha256(text)
            except Exception:
                summary["errors"] += 1
                continue

            prior = conn.execute(
                "SELECT content_sha256 FROM unified_index WHERE slug=?",
                (slug,)).fetchone()
            if prior is not None and prior["content_sha256"] == sha:
                summary["skipped"] += 1
                continue

            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO unified_index "
                    "(slug, topic, page_type, title, snippet, frontmatter, path, "
                    " content_sha256, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(slug) DO UPDATE SET "
                    "topic=excluded.topic, page_type=excluded.page_type, "
                    "title=excluded.title, snippet=excluded.snippet, "
                    "frontmatter=excluded.frontmatter, path=excluded.path, "
                    "content_sha256=excluded.content_sha256, "
                    "updated_at=excluded.updated_at",
                    (slug, topic, page_type, title, snippet, frontmatter_json,
                     str(path), sha, ts),
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
    # WITHIN the synced roots. Scope by the topics present in this call's roots so a
    # row from an un-synced root is never touched (mirrors memory_export orphan
    # prune, memory_export.py:102-109). A row whose topic was synced this call but
    # whose slug vanished is an orphan.
    synced_topics = set()
    for root in roots:
        for _path, topic, _slug in _iter_pages(root):
            synced_topics.add(topic)
    if synced_topics:
        placeholders = ",".join("?" for _ in synced_topics)
        existing = conn.execute(
            f"SELECT slug FROM unified_index WHERE topic IN ({placeholders})",
            tuple(synced_topics)).fetchall()
        orphans = [r["slug"] for r in existing if r["slug"] not in seen_slugs]
        if orphans:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(
                    "DELETE FROM unified_index WHERE slug=?",
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
