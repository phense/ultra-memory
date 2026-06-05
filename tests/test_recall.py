"""Recall-Reflex Phase 1 — the public recall() primitive over unified_recall.

recall(signal_text) is the thin, fail-open, privacy-scoped wrapper that the
engineering UserPromptSubmit hook and the trading observation surfaces both call:
recognise a situation -> recall what we know about it -> act informed.
"""
import json

from ultra_memory import memory_lib, recall, retrieval_core, wiki_sync


def _fake_embedder():
    """Deterministic stand-in (no fastembed download). BM25 does the matching;
    this just satisfies the memory backend's embedder requirement."""
    def embed(texts):
        out = []
        for t in list(texts):
            v = [0.0] * retrieval_core.EMBED_DIM
            v[0] = float(len(t) % 7) + 1.0
            out.append(v)
        return out
    return embed


def _seed_wiki(tmp_path, slug, title, body, *, topic="trading"):
    """Populate a fresh tmp DB's unified_index with one page; return the conn."""
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    d = tmp_path / "wiki" / topic / "concepts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.md").write_text(
        f"---\ntype: mechanism\ntitle: {title}\n---\n\n{body}\n", encoding="utf-8")
    wiki_sync.wiki_sync(conn, [tmp_path / "wiki"], embedder=None, ts=1)
    return conn


def test_recall_returns_topk_knowledge_snippets(tmp_path):
    conn = _seed_wiki(
        tmp_path, "fastembed-x",
        "fastembed onnxruntime NoSuchFile model_optimized.onnx",
        "TMPDIR purge of the fastembed model cache; pin via persistent_cache_dir.")
    hits = recall.recall("onnxruntime NoSuchFile model_optimized.onnx",
                         conn=conn, embedder=_fake_embedder(), top_k=3,
                         caller_class="subagent", agent_topics=None)
    assert any(h.get("slug") == "fastembed-x" for h in hits)
    assert len(hits) <= 3
    top = next(h for h in hits if h.get("slug") == "fastembed-x")
    assert top["source_kind"] == "knowledge"
    assert "model_optimized.onnx" in top["title"]
    assert top.get("score") is not None
    conn.close()


def test_recall_knowledge_only_skips_memory_and_works_without_embedder(tmp_path):
    """The Tier-2 engineering-hook path: knowledge_only=True + no embedder must NOT
    raise (memory backend skipped, so no embedder requirement) and must return ONLY
    knowledge hits — privacy-safe by construction (no user/feedback/project memory)."""
    conn = _seed_wiki(tmp_path, "kpage", "alpha widget error trace",
                      "a body mentioning the widget failure")
    memory_lib.save_memory(conn, id="m-widget", type="project",
                           title="widget memory note",
                           body="a project memory mentioning widget", ts=1)
    hits = recall.recall("widget", conn=conn, build_embedder=False,
                         knowledge_only=True, top_k=5)
    assert hits, "expected the knowledge page via BM25"
    assert all(h["source_kind"] == "knowledge" for h in hits)
    assert any(h.get("slug") == "kpage" for h in hits)
    conn.close()


def test_recall_excludes_index_and_redirect_pages(tmp_path):
    """Navigational pages (type: index / redirect) are noise as 'prior art' — recall
    drops them and still returns the real atomic."""
    conn = memory_lib.open_memory_db(tmp_path / "m.db")
    d = tmp_path / "wiki" / "trading" / "concepts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "widget-atomic.md").write_text(
        "---\ntype: mechanism\ntitle: widget gizmo error trace\n---\n\nbody about the widget\n")
    # Use the REAL navigational page-types the wiki schema emits (theme-index /
    # master-index / redirect) — NOT a fictional `index`.
    (d / "widget-theme-index.md").write_text(
        "---\ntype: theme-index\ntitle: widget things index\n---\n\nlists widget pages\n")
    (d / "widget-redirect.md").write_text(
        "---\ntype: redirect\ntitle: widget redirect\n---\n\nsee the widget atomic\n")
    wiki_sync.wiki_sync(conn, [tmp_path / "wiki"], embedder=None, ts=1)
    hits = recall.recall("widget", conn=conn, build_embedder=False,
                         knowledge_only=True, top_k=10)
    slugs = [h.get("slug") for h in hits]
    assert "widget-atomic" in slugs
    assert "widget-theme-index" not in slugs
    assert "widget-redirect" not in slugs
    conn.close()


def test_recall_fail_open_returns_empty_on_bad_db(tmp_path):
    """A db_path that cannot be opened/queried -> [] (fail-open), never raises."""
    out = recall.recall("anything", db_path=str(tmp_path / "nonexistent" / "x.db"),
                        embedder=_fake_embedder(), top_k=3)
    assert out == []


def test_recall_cli_json_prints_hits(tmp_path, monkeypatch, capsys):
    conn = _seed_wiki(
        tmp_path, "cli-page", "widget gizmo error trace",
        "A body about the widget gizmo failure mode.")
    conn.close()
    monkeypatch.setenv("ULTRA_MEMORY_DB", str(tmp_path / "m.db"))
    rc = recall.main(["widget gizmo", "--top", "3", "--no-embed", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert any(h.get("slug") == "cli-page" for h in data)


def test_recall_cli_always_rc0_on_error(tmp_path, monkeypatch):
    """A bad db -> fail-open rc 0, no crash."""
    monkeypatch.setenv("ULTRA_MEMORY_DB", str(tmp_path / "nope" / "x.db"))
    assert recall.main(["q", "--no-embed", "--json"]) == 0
