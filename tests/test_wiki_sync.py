"""SP-3 Stage 5 — `unified_index` + `wiki_sync` (the Tier-1 wiki->memory mirror,
§5.4, D8).

`wiki_sync` is POPULATION ONLY (retrieval is Stage 6). It is the idempotent
maintenance step that mirrors Expert-Knowledge pages into the `unified_index`
table beside the memory tables, and embeds each page into the SHARED `embeddings`
table (target_kind='knowledge') reusing the content_sha256 cache invalidation.

PROJECT-AGNOSTIC (the central NFR of this stage): `wiki_sync(conn, wiki_roots, …)`
takes `wiki_roots` as a CONSUMER-FED iterable of root paths — exactly like Stage
3's `mirror_cross_store_links(conn, wiki_edges, …)`. The engine imports NOTHING
from Trading: no `wiki_topics`, no `topics-registry.yaml`. `topic` is derived
GENERICALLY from the path (the first component under the root); `page_type`/
`title`/`snippet` are parsed with generic markdown; `slug` = file stem.

`maintain.run` resolves the roots from an env hook (ULTRA_MEMORY_WIKI_ROOTS,
os.pathsep- or comma-separated). If UNSET/empty -> wiki_sync is skipped entirely,
so a pure-memory deployment with no expert-wiki is byte-identically unaffected.
"""
import sqlite3

from ultra_memory import maintain, memory_lib, retrieval_core, wiki_sync


# A tiny deterministic fake embedder (list[str] -> list[list[float]]) so tests
# never load a model. Each text -> a 384-dim vector whose first slot is a stable
# hash-derived float; enough to land a row in `embeddings`.
def _fake_embedder():
    calls = {"n": 0, "texts": []}

    def embed(texts):
        texts = list(texts)
        calls["n"] += 1
        calls["texts"].extend(texts)
        out = []
        for t in texts:
            v = [0.0] * retrieval_core.EMBED_DIM
            v[0] = float(len(t) % 7) + 1.0
            out.append(v)
        return out

    embed.calls = calls
    return embed


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def _page(text="", *, type_="concept", title=None):
    fm = ["---", f"type: {type_}"]
    if title is not None:
        fm.append(f"title: {title}")
    fm.append("---")
    return "\n".join(fm) + "\n\n" + text


def _make_wiki(root, topic, slug, body, *, type_="concept", title=None):
    d = root / topic / "concepts"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{slug}.md"
    p.write_text(_page(body, type_=type_, title=title), encoding="utf-8")
    return p


def _index_rows(conn):
    return conn.execute(
        "SELECT slug, topic, page_type, title, snippet, frontmatter, path, "
        "content_sha256, bm25_text, updated_at FROM unified_index ORDER BY slug"
    ).fetchall()


# A body whose useful term sits past the 400-char snippet cap. The first ~400
# chars are filler; the distinctive back-half term `quokkasaurus` only appears
# near the end, so it survives in bm25_text (full body) but NOT in snippet.
_FILLER = ("filler " * 80).strip()           # ~560 chars, well past 400
_BACK_HALF_BODY = _FILLER + " quokkasaurus tail term"


# ---------------------------------------------------------------------------
# 1. Sync upserts every page in a tmp wiki tree.
# ---------------------------------------------------------------------------

def test_sync_upserts_every_page(tmp_path):
    conn = _db(tmp_path)
    root = tmp_path / "wiki"
    _make_wiki(root, "trading", "german-tax-fence", "The year-end fence body.",
               type_="mechanism", title="German Tax Fence")
    _make_wiki(root, "trading", "credit-spreads", "Credit spread mechanics.")
    _make_wiki(root, "programming", "tdd", "Test-first discipline.")

    summary = wiki_sync.wiki_sync(conn, [root], ts="2026-05-31T10:00:00Z")
    rows = _index_rows(conn)
    assert {r["slug"] for r in rows} == {
        "german-tax-fence", "credit-spreads", "tdd"}
    assert summary["upserted"] == 3
    assert summary["skipped"] == 0
    assert summary["pruned"] == 0

    by_slug = {r["slug"]: r for r in rows}
    assert by_slug["german-tax-fence"]["page_type"] == "mechanism"
    assert by_slug["german-tax-fence"]["title"] == "German Tax Fence"
    assert by_slug["german-tax-fence"]["updated_at"] == "2026-05-31T10:00:00Z"
    # snippet is the rendered body (frontmatter stripped).
    assert "year-end fence" in by_slug["german-tax-fence"]["snippet"]
    assert "---" not in by_slug["german-tax-fence"]["snippet"]
    conn.close()


# ---------------------------------------------------------------------------
# 1b. SP-6 #6 (D11) — bm25_text holds the FULL body (no 400-char cap), while
#     snippet stays the 400-char display preview.
# ---------------------------------------------------------------------------

def test_bm25_text_holds_full_body_snippet_stays_capped(tmp_path):
    conn = _db(tmp_path)
    root = tmp_path / "wiki"
    _make_wiki(root, "trading", "longpage", _BACK_HALF_BODY)
    wiki_sync.wiki_sync(conn, [root], ts="2026-05-31T10:00:00Z")
    row = {r["slug"]: r for r in _index_rows(conn)}["longpage"]
    # snippet is the 400-char display preview -> the back-half term is truncated.
    assert len(row["snippet"]) == 400
    assert "quokkasaurus" not in row["snippet"]
    # bm25_text is the FULL collapsed body -> the back-half term survives.
    assert "quokkasaurus" in row["bm25_text"]
    assert len(row["bm25_text"]) > 400
    # bm25_text is the whitespace-collapsed body (no raw frontmatter, no newlines).
    assert "\n" not in row["bm25_text"]
    assert "---" not in row["bm25_text"]
    conn.close()


# ---------------------------------------------------------------------------
# 2. Topic is derived GENERICALLY from the path (no wiki_topics import).
# ---------------------------------------------------------------------------

def test_topic_derived_from_path(tmp_path):
    conn = _db(tmp_path)
    root = tmp_path / "wiki"
    _make_wiki(root, "trading", "a", "x")
    _make_wiki(root, "programming", "b", "y")
    wiki_sync.wiki_sync(conn, [root], ts="2026-05-31T10:00:00Z")
    by_slug = {r["slug"]: r for r in _index_rows(conn)}
    assert by_slug["a"]["topic"] == "trading"
    assert by_slug["b"]["topic"] == "programming"
    conn.close()


def test_title_falls_back_to_heading_then_slug(tmp_path):
    conn = _db(tmp_path)
    root = tmp_path / "wiki"
    # No frontmatter title -> first '# heading'.
    _make_wiki(root, "trading", "with-heading", "# A Real Heading\n\nbody")
    # No title, no heading -> slug.
    d = root / "trading" / "concepts"
    (d / "bare.md").write_text("just body text, no fm, no heading\n",
                               encoding="utf-8")
    wiki_sync.wiki_sync(conn, [root], ts="2026-05-31T10:00:00Z")
    by_slug = {r["slug"]: r for r in _index_rows(conn)}
    assert by_slug["with-heading"]["title"] == "A Real Heading"
    assert by_slug["bare"]["title"] == "bare"
    conn.close()


# ---------------------------------------------------------------------------
# 3. A 2nd sync is a near no-op (sha skip; 0 re-embeds).
# ---------------------------------------------------------------------------

def test_second_sync_is_a_noop(tmp_path):
    conn = _db(tmp_path)
    root = tmp_path / "wiki"
    _make_wiki(root, "trading", "a", "stable body")
    _make_wiki(root, "trading", "b", "another body")
    emb = _fake_embedder()
    s1 = wiki_sync.wiki_sync(conn, [root], embedder=emb, ts="2026-05-31T10:00:00Z")
    assert s1["upserted"] == 2 and s1["embedded"] == 2
    calls_after_first = emb.calls["n"]

    s2 = wiki_sync.wiki_sync(conn, [root], embedder=emb, ts="2026-05-31T11:00:00Z")
    assert s2["upserted"] == 0
    assert s2["skipped"] == 2
    assert s2["embedded"] == 0          # sha unchanged -> no re-embed
    assert emb.calls["n"] == calls_after_first  # embedder not re-called for misses
    # updated_at stays at the first sync's ts (skipped rows are untouched).
    assert all(r["updated_at"] == "2026-05-31T10:00:00Z" for r in _index_rows(conn))
    conn.close()


def test_changed_page_re_upserts_and_re_embeds(tmp_path):
    conn = _db(tmp_path)
    root = tmp_path / "wiki"
    p = _make_wiki(root, "trading", "a", "first body")
    emb = _fake_embedder()
    wiki_sync.wiki_sync(conn, [root], embedder=emb, ts="2026-05-31T10:00:00Z")
    n_first = emb.calls["n"]
    # Mutate the file -> new content_sha256 -> re-upsert + re-embed.
    p.write_text(_page("second, longer body"), encoding="utf-8")
    s = wiki_sync.wiki_sync(conn, [root], embedder=emb, ts="2026-05-31T12:00:00Z")
    assert s["upserted"] == 1 and s["skipped"] == 0 and s["embedded"] == 1
    assert emb.calls["n"] == n_first + 1
    row = _index_rows(conn)[0]
    assert "second" in row["snippet"]
    assert row["updated_at"] == "2026-05-31T12:00:00Z"
    conn.close()


# ---------------------------------------------------------------------------
# 4. Reconciliation prunes a row after its file is deleted (within synced roots).
# ---------------------------------------------------------------------------

def test_reconciliation_prunes_orphans(tmp_path):
    conn = _db(tmp_path)
    root = tmp_path / "wiki"
    pa = _make_wiki(root, "trading", "a", "body a")
    _make_wiki(root, "trading", "b", "body b")
    wiki_sync.wiki_sync(conn, [root], ts="2026-05-31T10:00:00Z")
    assert {r["slug"] for r in _index_rows(conn)} == {"a", "b"}
    pa.unlink()
    s = wiki_sync.wiki_sync(conn, [root], ts="2026-05-31T11:00:00Z")
    assert s["pruned"] == 1
    assert {r["slug"] for r in _index_rows(conn)} == {"b"}
    conn.close()


def test_reconciliation_only_prunes_within_synced_roots(tmp_path):
    """A row that belongs to a root NOT in this sync call is left untouched —
    the orphan prune is scoped to the synced roots' topics/paths."""
    conn = _db(tmp_path)
    root_a = tmp_path / "wikiA"
    root_b = tmp_path / "wikiB"
    _make_wiki(root_a, "trading", "from-a", "body")
    _make_wiki(root_b, "research", "from-b", "body")
    # Seed both into the index.
    wiki_sync.wiki_sync(conn, [root_a, root_b], ts="2026-05-31T10:00:00Z")
    assert {r["slug"] for r in _index_rows(conn)} == {"from-a", "from-b"}
    # Now sync ONLY root_a (root_b is not in this call). from-b must survive.
    s = wiki_sync.wiki_sync(conn, [root_a], ts="2026-05-31T11:00:00Z")
    assert s["pruned"] == 0
    assert {r["slug"] for r in _index_rows(conn)} == {"from-a", "from-b"}
    conn.close()


# ---------------------------------------------------------------------------
# 4b. SP-6 #6 (D11) — `rebuild=True` backfills bm25_text on an existing row in
#     one pass, even though its content_sha256 is unchanged (the un-migrated /
#     pre-fix row carries a NULL bm25_text the sha-skip would otherwise leave).
# ---------------------------------------------------------------------------

def test_rebuild_backfills_bm25_text_on_unchanged_rows(tmp_path):
    conn = _db(tmp_path)
    root = tmp_path / "wiki"
    _make_wiki(root, "trading", "longpage", _BACK_HALF_BODY)
    wiki_sync.wiki_sync(conn, [root], ts="2026-05-31T10:00:00Z")
    # Simulate a row written by the PRE-fix wiki_sync: bm25_text is NULL, content
    # unchanged. A normal re-sync would sha-skip it and never populate bm25_text.
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE unified_index SET bm25_text=NULL WHERE slug='longpage'")
    conn.execute("COMMIT")
    assert conn.execute(
        "SELECT bm25_text FROM unified_index WHERE slug='longpage'"
    ).fetchone()["bm25_text"] is None

    # A plain re-sync sha-skips (does NOT backfill).
    s_skip = wiki_sync.wiki_sync(conn, [root], ts="2026-05-31T11:00:00Z")
    assert s_skip["skipped"] == 1
    assert conn.execute(
        "SELECT bm25_text FROM unified_index WHERE slug='longpage'"
    ).fetchone()["bm25_text"] is None

    # rebuild=True forces re-population in one pass -> bm25_text now full body.
    s_rebuild = wiki_sync.wiki_sync(conn, [root], rebuild=True,
                                    ts="2026-05-31T12:00:00Z")
    assert s_rebuild["upserted"] == 1
    assert s_rebuild["skipped"] == 0
    row = {r["slug"]: r for r in _index_rows(conn)}["longpage"]
    assert "quokkasaurus" in row["bm25_text"]
    conn.close()


# ---------------------------------------------------------------------------
# 5. Knowledge embeddings land in `embeddings` with target_kind='knowledge'.
# ---------------------------------------------------------------------------

def test_embeddings_land_with_knowledge_kind(tmp_path):
    conn = _db(tmp_path)
    root = tmp_path / "wiki"
    _make_wiki(root, "trading", "a", "embed me")
    _make_wiki(root, "trading", "b", "and me")
    s = wiki_sync.wiki_sync(conn, [root], embedder=_fake_embedder(),
                            ts="2026-05-31T10:00:00Z")
    assert s["embedded"] == 2
    rows = conn.execute(
        "SELECT target_kind, target_id, dim FROM embeddings "
        "WHERE target_kind='knowledge' ORDER BY target_id").fetchall()
    assert [r["target_id"] for r in rows] == ["a", "b"]
    assert all(r["target_kind"] == "knowledge" for r in rows)
    assert all(r["dim"] == retrieval_core.EMBED_DIM for r in rows)
    conn.close()


def test_no_embedder_still_upserts_index_rows(tmp_path):
    """embedder=None -> skip embedding but still populate unified_index."""
    conn = _db(tmp_path)
    root = tmp_path / "wiki"
    _make_wiki(root, "trading", "a", "body")
    s = wiki_sync.wiki_sync(conn, [root], embedder=None, ts="2026-05-31T10:00:00Z")
    assert s["upserted"] == 1
    assert s["embedded"] == 0
    assert {r["slug"] for r in _index_rows(conn)} == {"a"}
    # No knowledge embeddings written.
    n = conn.execute(
        "SELECT COUNT(*) FROM embeddings WHERE target_kind='knowledge'").fetchone()[0]
    assert n == 0
    conn.close()


# ---------------------------------------------------------------------------
# 6. Fail-open: a malformed page / missing root / embed failure never crashes.
# ---------------------------------------------------------------------------

def test_fail_open_on_malformed_page(tmp_path):
    conn = _db(tmp_path)
    root = tmp_path / "wiki"
    _make_wiki(root, "trading", "good", "fine body")
    # A file with bytes that explode a strict utf-8 read.
    bad = root / "trading" / "concepts" / "bad.md"
    bad.write_bytes(b"\xff\xfe not valid utf-8 \xff")
    s = wiki_sync.wiki_sync(conn, [root], ts="2026-05-31T10:00:00Z")
    # The good page is still indexed; the bad one is skipped (counted), no crash.
    slugs = {r["slug"] for r in _index_rows(conn)}
    assert "good" in slugs
    assert s.get("errors", 0) >= 1
    conn.close()


def test_fail_open_on_missing_root(tmp_path):
    conn = _db(tmp_path)
    missing = tmp_path / "does-not-exist"
    s = wiki_sync.wiki_sync(conn, [missing], ts="2026-05-31T10:00:00Z")
    assert s["upserted"] == 0 and s["pruned"] == 0
    assert _index_rows(conn) == []
    conn.close()


def test_fail_open_on_embed_failure(tmp_path):
    conn = _db(tmp_path)
    root = tmp_path / "wiki"
    _make_wiki(root, "trading", "a", "body")

    def boom(texts):
        raise RuntimeError("embedder exploded")

    s = wiki_sync.wiki_sync(conn, [root], embedder=boom, ts="2026-05-31T10:00:00Z")
    # The index row still landed (upsert is independent of the embed step).
    assert s["upserted"] == 1
    assert {r["slug"] for r in _index_rows(conn)} == {"a"}
    assert s.get("errors", 0) >= 1
    conn.close()


# ---------------------------------------------------------------------------
# 7. PROJECT-AGNOSTIC NFR — the new module imports NOTHING from Trading.
# ---------------------------------------------------------------------------

def test_wiki_sync_module_has_no_trading_import():
    import pathlib
    import re
    src = pathlib.Path(wiki_sync.__file__).read_text(encoding="utf-8")
    banned = re.compile(r"\b(wiki_topics|wiki_lib|wiki_query|trading_strategies|"
                        r"topics_registry|yaml)\b")
    offenders = [
        line.strip() for line in src.splitlines()
        if (line.strip().startswith("import ") or line.strip().startswith("from "))
        and banned.search(line)]
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# 8. Wiring into maintain.run — env hook + the pure-memory no-wiki skip.
# ---------------------------------------------------------------------------

def test_maintain_run_no_wiki_roots_is_byte_identical(tmp_path, monkeypatch):
    """The central no-wiki guarantee: maintain.run with NO wiki-roots config does
    EXACTLY what it did before Stage 5 — same return shape, no wiki_sync side
    effects (the unified_index stays empty)."""
    monkeypatch.delenv("ULTRA_MEMORY_WIKI_ROOTS", raising=False)
    db = tmp_path / "m.db"
    conn = memory_lib.open_memory_db(str(db))
    memory_lib.record_session_event(conn, session_id="s1", kind="task_done",
                                    title="recent", ts="2026-05-30T00:00:00Z")
    out = tmp_path / "export"
    res = maintain.run(conn, out_dir=str(out), ts="2026-05-31T12:00:00Z",
                       keep_days=90, force=True)
    # Same keys/values as today (no wiki_sync summary added when roots are unset).
    assert res == {"pruned": 0, "exported": True, "skipped": False}
    assert "wiki_sync" not in res
    # unified_index untouched.
    assert conn.execute("SELECT COUNT(*) FROM unified_index").fetchone()[0] == 0
    conn.close()


def test_maintain_run_with_wiki_roots_runs_sync(tmp_path, monkeypatch):
    db = tmp_path / "m.db"
    conn = memory_lib.open_memory_db(str(db))
    root = tmp_path / "wiki"
    _make_wiki(root, "trading", "a", "body a")
    _make_wiki(root, "trading", "b", "body b")
    monkeypatch.setenv("ULTRA_MEMORY_WIKI_ROOTS", str(root))
    out = tmp_path / "export"
    res = maintain.run(conn, out_dir=str(out), ts="2026-05-31T12:00:00Z",
                       keep_days=90, force=True)
    assert res["skipped"] is False
    # The wiki_sync summary is surfaced in run's return (Risk §14.4 reconciliation).
    assert "wiki_sync" in res
    assert res["wiki_sync"]["upserted"] == 2
    assert {r["slug"] for r in _index_rows(conn)} == {"a", "b"}
    conn.close()


def test_maintain_run_throttled_skips_sync_too(tmp_path, monkeypatch):
    """wiki_sync lives INSIDE the existing throttle (no second throttle) — a
    throttled run does not run it."""
    db = tmp_path / "m.db"
    conn = memory_lib.open_memory_db(str(db))
    root = tmp_path / "wiki"
    _make_wiki(root, "trading", "a", "body")
    monkeypatch.setenv("ULTRA_MEMORY_WIKI_ROOTS", str(root))
    out = tmp_path / "export"
    maintain.run(conn, out_dir=str(out), ts="2026-05-31T12:00:00Z", force=True)
    # Index now populated; clear it to detect a 2nd sync.
    conn.execute("DELETE FROM unified_index")
    conn.commit()
    res = maintain.run(conn, out_dir=str(out), ts="2026-05-31T17:00:00Z",
                       force=False)
    assert res["skipped"] is True
    # Throttled -> wiki_sync did NOT re-run -> index stays empty.
    assert conn.execute("SELECT COUNT(*) FROM unified_index").fetchone()[0] == 0
    conn.close()


def test_resolve_wiki_roots_parses_pathsep_and_comma(monkeypatch):
    import os
    monkeypatch.setenv("ULTRA_MEMORY_WIKI_ROOTS", f"/a{os.pathsep}/b,/c")
    roots = maintain._resolve_wiki_roots(os.environ)
    assert [str(r) for r in roots] == ["/a", "/b", "/c"]


def test_resolve_wiki_roots_empty_is_empty(monkeypatch):
    import os
    monkeypatch.delenv("ULTRA_MEMORY_WIKI_ROOTS", raising=False)
    assert maintain._resolve_wiki_roots(os.environ) == []
    monkeypatch.setenv("ULTRA_MEMORY_WIKI_ROOTS", "   ")
    assert maintain._resolve_wiki_roots(os.environ) == []
