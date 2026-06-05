"""Recall-Reflex Phase 2 — the `## Signal` retrieval channel (the keystone).

`## Signal` is an optional H2 section: the observable condition under which a page
should be recalled. It is indexed as a DISTINCT embedding channel
(target_kind='knowledge_signal') and fused as a SEPARATE RRF backend in
unified_recall — so a page whose recorded observable matches the query earns extra
rank credit (the "boost"), reusing the existing scale-invariant RRF with no new
scoring math.
"""
from ultra_memory import memory_lib, retrieval_core, unified_query, wiki_sync


def _fake_embedder():
    """All-aligned unit vectors -> cosine 1.0 for every page that HAS a vector.
    The boost test relies on this: ranking differences then come ONLY from which
    RRF backends an item appears in, not from cosine magnitude."""
    def embed(texts):
        out = []
        for _t in list(texts):
            v = [0.0] * retrieval_core.EMBED_DIM
            v[0] = 1.0
            out.append(v)
        return out
    return embed


def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def _write(root, topic, slug, body, *, title="T"):
    d = root / topic / "concepts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.md").write_text(
        f"---\ntype: mechanism\ntitle: {title}\n---\n\n{body}\n", encoding="utf-8")


# --- 2.1 extract_signal_text ------------------------------------------------

def test_extract_signal_text_parses_the_h2_section():
    body = ("Intro prose.\n\n## Signal\n\nonnxruntime NoSuchFile model.onnx purge\n\n"
            "## Mechanism\n\nThe fix.\n")
    assert (wiki_sync.extract_signal_text(body)
            == "onnxruntime NoSuchFile model.onnx purge")


def test_extract_signal_text_absent_or_empty_returns_none():
    assert wiki_sync.extract_signal_text("No signal here.\n\n## Mechanism\n\nx") is None
    assert wiki_sync.extract_signal_text("") is None
    assert wiki_sync.extract_signal_text("## Signal\n\n   \n\n## Mechanism\n") is None


# --- 2.2 wiki_sync embeds the ## Signal channel -----------------------------

def test_wiki_sync_embeds_signal_as_distinct_channel(tmp_path):
    conn = _db(tmp_path)
    root = tmp_path / "wiki"
    _write(root, "trading", "sig-page", "Body.\n\n## Signal\n\nthe observable text\n")
    _write(root, "trading", "plain-page", "Just body, no signal section.")
    s = wiki_sync.wiki_sync(conn, [root], embedder=_fake_embedder(), ts=1)
    assert s["embedded_signal"] == 1
    rows = conn.execute(
        "SELECT target_id FROM embeddings WHERE target_kind='knowledge_signal'"
    ).fetchall()
    assert {r["target_id"] for r in rows} == {"sig-page"}
    conn.close()


def test_wiki_sync_drops_stale_signal_vector_when_signal_removed(tmp_path):
    conn = _db(tmp_path)
    root = tmp_path / "wiki"
    _write(root, "trading", "p", "Body.\n\n## Signal\n\nobservable\n")
    wiki_sync.wiki_sync(conn, [root], embedder=_fake_embedder(), ts=1)
    assert conn.execute(
        "SELECT COUNT(*) FROM embeddings WHERE target_kind='knowledge_signal'"
    ).fetchone()[0] == 1
    _write(root, "trading", "p", "Body only now, no signal section.")  # edit removes it
    wiki_sync.wiki_sync(conn, [root], embedder=_fake_embedder(), ts=2)
    assert conn.execute(
        "SELECT COUNT(*) FROM embeddings WHERE target_kind='knowledge_signal'"
    ).fetchone()[0] == 0
    conn.close()


# --- 2.3 the signal backend boosts ranking ----------------------------------

def test_signal_backend_boosts_a_signal_keyed_page_above_a_plain_page(tmp_path):
    conn = _db(tmp_path)
    root = tmp_path / "wiki"
    # Neither body contains the query terms -> BM25 contributes nothing; both pages
    # are reachable only via the main embed backend (cosine 1.0). ONLY with-signal
    # also has a signal vector -> an extra RRF backend credit -> it ranks first.
    _write(root, "trading", "with-signal",
           "alpha beta gamma\n\n## Signal\n\ndelta epsilon\n", title="With signal")
    _write(root, "trading", "no-signal",
           "alpha beta gamma content here", title="No signal")
    wiki_sync.wiki_sync(conn, [root], embedder=_fake_embedder(), ts=1)
    hits = unified_query.unified_recall(
        conn, "zzz query phrase not in any body", caller_class="orchestrator",
        agent_topics=None, embedder=_fake_embedder(), top_k=5, audit=False)
    slugs = [h.get("slug") for h in hits]
    assert "with-signal" in slugs and "no-signal" in slugs
    assert slugs.index("with-signal") < slugs.index("no-signal")
    conn.close()
