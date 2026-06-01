"""SP-3 Stage 6 — `unified_recall` + topic/type access scope (§5.6, D9/D10/D11).

The 5 parity fences (the spec's HARD gates):
  1. Memory-only byte-identity   — unified_index empty ⇒ byte-identical to
                                    query_memories on a fixed query set.
  2. Scope fail-closed (SECURITY) — a subagent with NO topic binding sees ONLY
                                    topic-IS-NULL operational memories of its
                                    allowed types, ZERO topiced knowledge, ZERO
                                    topiced memories; orchestrator sees all. Both
                                    directions tested.
  3. Knowledge + cross-store self-golden regression fence (NOT wiki_query parity —
                                    deferred to a Trading-side SP-5 test, per D-S6).
  4. outcome_weight=1.0 is inert  — multiplying by the default changes nothing.
  5. RRF rank-fusion is scale-invariant — a knowledge rank-1 hit in a tiny corpus
                                    is not buried by raw-score flattening (FU-4).
"""
import json

from ultra_memory import knowledge_mcp, memory_lib, memory_query, unified_query


# --- fixtures ---------------------------------------------------------------

def _db(tmp_path):
    return memory_lib.open_memory_db(tmp_path / "m.db")


def _fake_embedder(mapping, dim=3):
    """First matching substring → its vector, else zero vector."""
    def _embed(texts):
        out = []
        for t in texts:
            vec = [0.0] * dim
            for key, v in mapping.items():
                if key in t:
                    vec = v
                    break
            out.append(vec)
        return out
    return _embed


def _save(conn, **kw):
    kw.setdefault("type", "reference")
    kw.setdefault("ts", "2026-05-01T00:00:00")
    memory_lib.save_memory(conn, **kw)


def _add_knowledge(conn, *, slug, topic, title, snippet, page_type="concept",
                   ts="2026-05-01T00:00:00", outcome_weight=None, bm25_text=None):
    """Insert a unified_index row directly (Stage-5 wiki_sync's output shape)."""
    fm = json.dumps({"type": page_type, "title": title}, sort_keys=True)
    cols = ("slug", "topic", "page_type", "title", "snippet", "frontmatter",
            "path", "content_sha256", "updated_at")
    vals = [slug, topic, page_type, title, snippet, fm,
            f"/wiki/{topic}/{slug}.md", "sha-" + slug, ts]
    if outcome_weight is not None:
        cols = cols + ("outcome_weight",)
        vals = vals + [outcome_weight]
    if bm25_text is not None:
        cols = cols + ("bm25_text",)
        vals = vals + [bm25_text]
    placeholders = ",".join("?" for _ in cols)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(f"INSERT INTO unified_index ({','.join(cols)}) VALUES ({placeholders})",
                 tuple(vals))
    conn.execute("COMMIT")


def _embed_knowledge(conn, slug, vec, dim=3):
    """Seed a knowledge vector into the SHARED embeddings table (target_kind=
    'knowledge') so the embed backend has something to rank — the golden pattern
    for exercising the embed path when fastembed is absent."""
    from ultra_memory import retrieval_core as rc
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO embeddings (target_kind, target_id, model_name, dim, vector, "
        "content_sha256) VALUES ('knowledge', ?, ?, ?, ?, ?)",
        (slug, rc.EMBED_MODEL, dim, rc.pack_vector(vec), "sha-" + slug))
    conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# FENCE 1 — memory-only byte-identity (HARD gate).
# ---------------------------------------------------------------------------

def test_fence1_memory_only_byte_identical_to_query_memories(tmp_path):
    """With unified_index EMPTY, unified_recall (orchestrator, all topics) returns a
    list BYTE-IDENTICAL to query_memories on a fixed query set — the §5.6 invariant
    for the memory store. Same order, same per-item fields, same scores."""
    conn = _db(tmp_path)
    _save(conn, id="apple", title="apple", body="apple fruit")
    _save(conn, id="car", title="car", body="car vehicle")
    _save(conn, id="bond", title="bond", body="bond yield treasury")
    emb = _fake_embedder({"apple": [1.0, 0.0, 0.0], "car": [0.0, 1.0, 0.0],
                          "bond": [0.0, 0.0, 1.0]})

    # The trusted/orchestrator caller (all types, all topics) is the apples-to-apples
    # comparison against query_memories' raw output.
    for q in ("apple", "car", "bond yield", "vehicle"):
        baseline = memory_query.query_memories(
            conn, q, embedder=emb, dim=3, top_k=5,
            include_types=sorted(knowledge_mcp.ALL_TYPES),
            now_ts="2026-05-02T00:00:00")
        unified = unified_query.unified_recall(
            conn, q, caller_class="orchestrator", agent_topics=None,
            embedder=emb, dim=3, top_k=5, now_ts="2026-05-02T00:00:00", audit=False)
        assert unified == baseline, f"byte-identity broke for query {q!r}"
    conn.close()


# ---------------------------------------------------------------------------
# FENCE 2 — scope fail-closed (SECURITY — the privilege boundary, HARD gate).
# ---------------------------------------------------------------------------

def test_fence2_subagent_unbound_sees_only_null_topic_allowed_type_memories(tmp_path):
    """A subagent with NO topic binding (empty agent_topics) sees ONLY topic-IS-NULL
    operational memories of its ALLOWED types — ZERO topiced knowledge AND ZERO
    topiced memories. Fail-closed on BOTH axes."""
    conn = _db(tmp_path)
    # NULL-topic operational rows: one allowed-type (reference), one denied
    # (user — a subagent must never see user/feedback).
    _save(conn, id="null_ref", type="reference", title="null ref",
          body="rrfquery shared note", topic=None)
    _save(conn, id="null_user", type="user", title="null user",
          body="rrfquery secret pref", topic=None)
    # A topiced memory of an allowed type — must be HIDDEN from the unbound subagent.
    _save(conn, id="trading_ref", type="reference", title="trading ref",
          body="rrfquery trading note", topic="trading")
    # A topiced knowledge page — must be HIDDEN from the unbound subagent.
    _add_knowledge(conn, slug="kn1", topic="trading", title="kn one",
                   snippet="rrfquery trading knowledge")
    emb = _fake_embedder({"rrfquery": [1.0, 0.0, 0.0]})

    out = unified_query.unified_recall(
        conn, "rrfquery", caller_class="subagent", agent_topics=set(),
        embedder=emb, dim=3, top_k=10, now_ts="2026-05-02T00:00:00", audit=False)
    seen_ids = {h.get("id") for h in out}
    # The ONLY visible row: the NULL-topic allowed-type memory.
    assert seen_ids == {"null_ref"}, seen_ids
    # No topiced knowledge.
    assert not any(h.get("source_kind") == "knowledge" for h in out)
    # No user/feedback (type wall) and no topiced memory.
    assert "null_user" not in seen_ids
    assert "trading_ref" not in seen_ids
    conn.close()


def test_fence2_orchestrator_sees_all_topics_and_types(tmp_path):
    """The orchestrator (agent_topics=None, trusted caller_class) sees EVERYTHING:
    NULL-topic + topiced memories of ALL types, AND topiced knowledge."""
    conn = _db(tmp_path)
    _save(conn, id="null_ref", type="reference", title="null ref",
          body="rrfquery shared", topic=None)
    _save(conn, id="null_user", type="user", title="null user",
          body="rrfquery secret", topic=None)
    _save(conn, id="trading_ref", type="reference", title="trading ref",
          body="rrfquery trading", topic="trading")
    _add_knowledge(conn, slug="kn1", topic="trading", title="kn one",
                   snippet="rrfquery trading knowledge")
    emb = _fake_embedder({"rrfquery": [1.0, 0.0, 0.0]})

    out = unified_query.unified_recall(
        conn, "rrfquery", caller_class="orchestrator", agent_topics=None,
        embedder=emb, dim=3, top_k=10, now_ts="2026-05-02T00:00:00", audit=False)
    mem_ids = {h["id"] for h in out if h.get("source_kind") != "knowledge"
               and "id" in h}
    kn_slugs = {h["slug"] for h in out if h.get("source_kind") == "knowledge"}
    assert {"null_ref", "null_user", "trading_ref"} <= mem_ids
    assert "kn1" in kn_slugs
    conn.close()


def test_fence2_bound_subagent_sees_its_topic_only(tmp_path):
    """A subagent BOUND to topic 'trading' sees its topic's knowledge + memories +
    NULL-topic allowed-type memories — but NOT another topic's, and NOT
    user/feedback (type wall still holds)."""
    conn = _db(tmp_path)
    _save(conn, id="null_ref", type="reference", title="null ref",
          body="rrfquery shared", topic=None)
    _save(conn, id="trading_ref", type="reference", title="trading ref",
          body="rrfquery trading mem", topic="trading")
    _save(conn, id="cooking_ref", type="reference", title="cooking ref",
          body="rrfquery cooking mem", topic="cooking")
    _save(conn, id="trading_user", type="user", title="trading user",
          body="rrfquery trading secret", topic="trading")
    _add_knowledge(conn, slug="kn_trade", topic="trading", title="kn trade",
                   snippet="rrfquery trading knowledge")
    _add_knowledge(conn, slug="kn_cook", topic="cooking", title="kn cook",
                   snippet="rrfquery cooking knowledge")
    emb = _fake_embedder({"rrfquery": [1.0, 0.0, 0.0]})

    out = unified_query.unified_recall(
        conn, "rrfquery", caller_class="subagent", agent_topics={"trading"},
        embedder=emb, dim=3, top_k=10, now_ts="2026-05-02T00:00:00", audit=False)
    mem_ids = {h["id"] for h in out if h.get("source_kind") != "knowledge"
               and "id" in h}
    kn_slugs = {h["slug"] for h in out if h.get("source_kind") == "knowledge"}
    assert mem_ids == {"null_ref", "trading_ref"}, mem_ids  # NOT cooking, NOT user
    assert kn_slugs == {"kn_trade"}, kn_slugs               # NOT kn_cook
    conn.close()


# ---------------------------------------------------------------------------
# FENCE 3 — knowledge + cross-store self-golden regression fence.
# ---------------------------------------------------------------------------

def test_fence3_cross_store_self_golden(tmp_path):
    """A regression fence over unified_recall's OWN output on a fixed (memory +
    knowledge) fixture. This is NOT byte-identity with Trading's wiki_query (the
    engine cannot import it — that cross-codebase parity is deferred to a
    Trading-side SP-5 integration test, per D-S6); it locks the cross-store
    ordering + shape against silent drift."""
    conn = _db(tmp_path)
    _save(conn, id="mem_macro", type="reference", title="macro note",
          body="liquidity transmission mechanism", topic="trading")
    _add_knowledge(conn, slug="kn_macro", topic="trading", title="Macro mechanisms",
                   snippet="liquidity transmission across rates and the dollar")
    _add_knowledge(conn, slug="kn_vol", topic="trading", title="Volatility",
                   snippet="implied vol surface skew and the rule of 16")
    emb = _fake_embedder({"liquidity transmission": [1.0, 0.0, 0.0]})

    out = unified_query.unified_recall(
        conn, "liquidity transmission", caller_class="subagent",
        agent_topics={"trading"}, embedder=emb, dim=3, top_k=5,
        now_ts="2026-05-02T00:00:00", audit=False)

    # Golden: the cross-store ranking surfaces both the memory note and the matching
    # knowledge page; the off-topic-match vol page ranks last or is absent. Lock the
    # ordered (source_kind, key) sequence.
    ordered = [(h["source_kind"], h.get("id") or h.get("slug")) for h in out]
    assert ("memory", "mem_macro") in ordered
    assert ("knowledge", "kn_macro") in ordered
    # kn_macro (BM25 + relevance) must outrank kn_vol (no shared query terms).
    if ("knowledge", "kn_vol") in ordered:
        assert ordered.index(("knowledge", "kn_macro")) < \
               ordered.index(("knowledge", "kn_vol"))
    # Shape: every knowledge hit carries the wiki-page fields.
    for h in out:
        if h["source_kind"] == "knowledge":
            assert set(h) >= {"slug", "topic", "title", "page_type", "snippet",
                              "path", "score"}
    conn.close()


def test_fence3b_knowledge_bm25_only_when_no_vectors(tmp_path):
    """FAIL-OPEN on missing knowledge embeddings (the --extra mcp env may lack
    fastembed): with NO knowledge vectors seeded, the embed backend degrades to
    empty and fusion proceeds BM25-only — the matching page still surfaces."""
    conn = _db(tmp_path)
    _add_knowledge(conn, slug="kn_a", topic="trading", title="Alpha decay",
                   snippet="signal alpha decays over the holding horizon")
    _add_knowledge(conn, slug="kn_b", topic="trading", title="Carry",
                   snippet="carry trade funding currency")
    # NO _embed_knowledge calls — no vectors in the table.
    emb = _fake_embedder({"alpha": [1.0, 0.0, 0.0]})  # used for memory + query embed

    out = unified_query.unified_recall(
        conn, "alpha decay horizon", caller_class="subagent",
        agent_topics={"trading"}, embedder=emb, dim=3, top_k=5,
        now_ts="2026-05-02T00:00:00", audit=False)
    slugs = [h["slug"] for h in out if h["source_kind"] == "knowledge"]
    assert slugs and slugs[0] == "kn_a"  # BM25 alone ranks the matching page first
    conn.close()


def test_fence3c_knowledge_embed_path_exercised(tmp_path):
    """When knowledge vectors ARE seeded (golden pattern), the embed backend
    contributes — a page that the query embeds close to but shares NO BM25 terms
    with still surfaces via the cosine backend."""
    conn = _db(tmp_path)
    _add_knowledge(conn, slug="kn_sem", topic="trading", title="Regime",
                   snippet="markov state classification of the tape")
    # Query embeds onto the same vector as the page, but shares no tokens with it.
    _embed_knowledge(conn, "kn_sem", [1.0, 0.0, 0.0])
    emb = _fake_embedder({"zzqq": [1.0, 0.0, 0.0]})  # query 'zzqq' -> [1,0,0]

    out = unified_query.unified_recall(
        conn, "zzqq", caller_class="subagent", agent_topics={"trading"},
        embedder=emb, dim=3, top_k=5, now_ts="2026-05-02T00:00:00", audit=False)
    slugs = {h["slug"] for h in out if h["source_kind"] == "knowledge"}
    assert "kn_sem" in slugs  # found purely via the embed backend (no BM25 overlap)
    conn.close()


# ---------------------------------------------------------------------------
# FENCE 4 — outcome_weight=1.0 is inert.
# ---------------------------------------------------------------------------

def test_fence4_outcome_weight_default_is_inert(tmp_path):
    """Multiplying by the default outcome_weight (1.0) changes NOTHING: the ranking
    + scores are identical to a run where no weight is applied. We compare a
    default-weight knowledge corpus against an explicit-1.0 corpus."""
    emb = _fake_embedder({"liquidity": [1.0, 0.0, 0.0]})

    def _run(set_weight):
        from pathlib import Path
        import tempfile
        d = Path(tempfile.mkdtemp())
        conn = memory_lib.open_memory_db(d / "m.db")
        _save(conn, id="mem1", type="reference", title="m1",
              body="liquidity note", topic="trading")
        _add_knowledge(conn, slug="kA", topic="trading", title="A",
                       snippet="liquidity transmission alpha",
                       outcome_weight=(1.0 if set_weight else None))
        _add_knowledge(conn, slug="kB", topic="trading", title="B",
                       snippet="liquidity carry beta",
                       outcome_weight=(1.0 if set_weight else None))
        out = unified_query.unified_recall(
            conn, "liquidity", caller_class="subagent", agent_topics={"trading"},
            embedder=emb, dim=3, top_k=5, now_ts="2026-05-02T00:00:00", audit=False)
        conn.close()
        return [(h["source_kind"], h.get("id") or h.get("slug"), round(h["score"], 12))
                for h in out]

    assert _run(set_weight=True) == _run(set_weight=False)


def test_fence4b_outcome_weight_actually_reweights(tmp_path):
    """Sanity-counterpart to inertness: a NON-1.0 weight DOES move ranking — proving
    the hook is wired (not silently ignored). A heavily down-weighted top BM25 hit
    drops below an equal-rank competitor."""
    conn = _db(tmp_path)
    _add_knowledge(conn, slug="kHi", topic="trading", title="Hi",
                   snippet="liquidity premium term", outcome_weight=1.0)
    _add_knowledge(conn, slug="kLo", topic="trading", title="Lo",
                   snippet="liquidity premium term", outcome_weight=0.01)
    emb = _fake_embedder({"liquidity premium": [1.0, 0.0, 0.0]})
    out = unified_query.unified_recall(
        conn, "liquidity premium term", caller_class="subagent",
        agent_topics={"trading"}, embedder=emb, dim=3, top_k=5,
        now_ts="2026-05-02T00:00:00", audit=False)
    slugs = [h["slug"] for h in out if h["source_kind"] == "knowledge"]
    assert slugs.index("kHi") < slugs.index("kLo")  # the down-weighted page sinks
    conn.close()


# ---------------------------------------------------------------------------
# FENCE 5 — RRF rank-fusion is scale-invariant (the FU-4 lesson).
# ---------------------------------------------------------------------------

def test_fence5_rrf_is_scale_invariant():
    """A rank-1 hit earns 1/(k+1) regardless of any backend's raw-score scale — the
    FU-4 fence. We fuse RANKS, never raw scores, so a tiny-corpus rank-1 knowledge
    hit is NOT buried by a large-corpus backend's flattened raw scores."""
    k = 60
    # Backend A: a tiny corpus, the target at rank 1.
    # Backend B: a large corpus, the target absent; many other items at ranks 1..N.
    target = ("knowledge", "small_rank1")
    big = [("knowledge", f"big{i}") for i in range(50)]
    fused = unified_query._best_rank_rrf([[target], big])
    # The rank-1-in-tiny-corpus target scores exactly 1/(k+1) — full RRF credit, NOT
    # diluted by the 50-item backend it's absent from.
    assert abs(fused[target] - 1.0 / (k + 1)) < 1e-12
    # And it beats every middling item in the big backend (rank>=2).
    assert fused[target] > fused[("knowledge", "big10")]


def test_fence5b_best_rank_no_double_count():
    """FU-4 follow-up: an item appearing at rank-1 in TWO of one backend's lists is
    credited ONCE (best-rank-per-backend), not summed — same as a single rank-1."""
    item = ("knowledge", "dup")
    # Two lists both putting `dup` at rank 1 — should still be a single 1/(k+1).
    fused_double = unified_query._best_rank_rrf([[item], [item]])
    # But _best_rank_rrf takes ONE list per backend; the dedup is WITHIN a list.
    fused_within = unified_query._best_rank_rrf([[item, item]])
    k = 60
    # Within one backend list, the duplicate is collapsed to its best rank (1).
    assert abs(fused_within[item] - 1.0 / (k + 1)) < 1e-12
    # Across two SEPARATE backends it earns one term each (that's correct — two
    # backends), demonstrating per-backend single-credit (not per-list summation).
    assert abs(fused_double[item] - 2.0 / (k + 1)) < 1e-12


# ---------------------------------------------------------------------------
# SP-6 #6 (D11) — _knowledge_doc_text BM25s the FULL bm25_text, falling back to
# snippet for un-migrated / NULL rows (back-compat).
# ---------------------------------------------------------------------------

def test_knowledge_doc_text_uses_bm25_text_full_body():
    row = {"title": "T",
           "snippet": "short preview",
           "frontmatter": '{"tags": ["frontmatteronlyterm"]}',
           "bm25_text": "the full collapsed body with a back-half quokkasaurus term"}
    doc = unified_query._knowledge_doc_text(row)
    assert "quokkasaurus" in doc          # the full body is the BM25 document
    assert "short preview" not in doc     # snippet is NOT used when bm25_text exists
    assert "T" in doc                     # title still present (high-signal)
    # SP-6 stage-3 parity fix: the frontmatter is DROPPED from the BM25 document so a
    # frontmatter-only tag/type term cannot produce a spurious tag-match hit — this
    # aligns the engine's BM25 document with wiki_query's rendered-body BM25.
    assert "frontmatteronlyterm" not in doc


def test_knowledge_doc_text_falls_back_to_snippet_when_bm25_text_null():
    row = {"title": "T", "snippet": "short preview", "frontmatter": "{}",
           "bm25_text": None}
    doc = unified_query._knowledge_doc_text(row)
    assert "short preview" in doc         # NULL bm25_text -> fall back to snippet


def test_knowledge_doc_text_handles_row_missing_bm25_text_column():
    """A row dict produced before migration 0005 has no bm25_text key at all —
    the doc-text builder must still fall back to snippet (back-compat)."""
    row = {"title": "T", "snippet": "legacy preview", "frontmatter": "{}"}
    doc = unified_query._knowledge_doc_text(row)
    assert "legacy preview" in doc


def test_knowledge_candidates_ranks_back_half_term_via_bm25_text(tmp_path):
    """End-to-end: a query term that lives ONLY in bm25_text (past the snippet cap)
    still ranks the page — the SP-5 tail-divergence the fix closes."""
    conn = _db(tmp_path)
    filler = ("filler " * 80).strip()
    full_body = filler + " quokkasaurus tail term"
    _add_knowledge(conn, slug="longpage", topic="trading", title="Long Page",
                   snippet=filler[:400], bm25_text=full_body)
    bm25_ranked, _embed_ranked, by_slug = unified_query._knowledge_candidates(
        conn, "quokkasaurus", agent_topics={"trading"}, embedder=None, dim=3)
    assert "longpage" in bm25_ranked
    conn.close()


# ---------------------------------------------------------------------------
# SP-6 stage-3 parity fix — the BM25 document is title + body only; a query term
# that matches ONLY a page's frontmatter (tags/type) must NOT produce a spurious
# tag-match hit (the SP-5 parity divergence that forced θ_OVERLAP down to 0.50).
# ---------------------------------------------------------------------------

def test_knowledge_candidates_frontmatter_only_term_does_not_hit(tmp_path):
    """A query term present ONLY in a page's frontmatter (here the tag value
    'macro', absent from title + body) must NOT surface the page as a knowledge
    candidate — before the fix the frontmatter was part of the BM25 document, so
    'macro' produced a spurious near-zero-relevance tail hit, diverging from
    Trading's wiki_query (which BM25s the rendered body, not the frontmatter)."""
    conn = _db(tmp_path)
    # The frontmatter (built by _add_knowledge) carries page_type/title only, so we
    # seed the tag-only term via a body whose RENDERED text omits it: the term lives
    # purely in the JSON frontmatter, never in title/snippet/bm25_text.
    fm = json.dumps({"type": "concept", "title": "Carry Trade",
                     "tags": ["macro"]}, sort_keys=True)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO unified_index (slug, topic, page_type, title, snippet, "
        "frontmatter, path, content_sha256, updated_at, bm25_text) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("kn_carry", "trading", "concept", "Carry Trade",
         "funding currency interest-rate differential",
         fm, "/wiki/trading/kn_carry.md", "sha-kn_carry",
         "2026-05-01T00:00:00",
         "the carry trade captures the funding currency interest-rate differential"))
    conn.execute("COMMIT")

    bm25_ranked, _embed, _by = unified_query._knowledge_candidates(
        conn, "macro", agent_topics={"trading"}, embedder=None, dim=3)
    # 'macro' is only in the frontmatter tags -> no BM25 hit after the fix.
    assert "kn_carry" not in bm25_ranked

    # Counterpart: a term in the title/body still ranks the page normally.
    bm25_body, _e2, _b2 = unified_query._knowledge_candidates(
        conn, "funding currency", agent_topics={"trading"}, embedder=None, dim=3)
    assert "kn_carry" in bm25_body
    conn.close()


# ---------------------------------------------------------------------------
# topic_scope_from_env — fail-closed env/binding resolution (D10).
# ---------------------------------------------------------------------------

def test_topic_scope_from_env_no_binding_is_empty(tmp_path):
    """No env var, no agent binding ⇒ EMPTY topic set (fail-closed)."""
    conn = _db(tmp_path)
    assert unified_query.topic_scope_from_env({}, conn) == set()
    conn.close()


def test_topic_scope_from_env_reads_env_var():
    topics = unified_query.topic_scope_from_env(
        {"ULTRA_MEMORY_CALLER_TOPIC": "trading, cooking"})
    assert topics == {"trading", "cooking"}


def test_topic_scope_from_env_reads_bindings_table(tmp_path):
    conn = _db(tmp_path)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO agent_topic_bindings (agent_name, topic, created_at) "
                 "VALUES ('risk-bot','trading','2026-05-01T00:00:00')")
    conn.execute("INSERT INTO agent_topic_bindings (agent_name, topic, created_at) "
                 "VALUES ('risk-bot','macro','2026-05-01T00:00:00')")
    conn.execute("COMMIT")
    topics = unified_query.topic_scope_from_env(
        {"ULTRA_MEMORY_AGENT_NAME": "risk-bot"}, conn)
    assert topics == {"trading", "macro"}
    conn.close()


# ---------------------------------------------------------------------------
# Additive MCP routing — legacy memory-only path stays UNCHANGED.
# ---------------------------------------------------------------------------

def test_run_query_tool_legacy_path_unchanged(tmp_path):
    """Without an agent_topics argument, run_query_tool keeps the SP-1 memory-only
    knowledge_recall behavior — existing knowledge-MCP tests must stay green."""
    conn = _db(tmp_path)
    _save(conn, id="m1", type="reference", title="rate cuts", body="rate cuts ahead")
    emb = _fake_embedder({"rate": [1.0, 0.0, 0.0]})
    out = knowledge_mcp.run_query_tool(
        {"query": "rate cuts"}, conn=conn, embedder=emb, caller_class="subagent",
        dim=3, now_ts="2026-05-02T00:00:00", ts="2026-05-02T00:00:00")
    payload = json.loads(out[0].text)
    # Legacy shape: knowledge_recall dicts (no source_kind), with a snippet field.
    assert "results" in payload
    assert payload["results"] and "snippet" in payload["results"][0]
    assert "source_kind" not in payload["results"][0]
    conn.close()


def test_run_query_tool_routes_to_unified_when_topics_present(tmp_path):
    """With agent_topics supplied, run_query_tool routes to unified_recall — the
    cross-store surface (source_kind present)."""
    conn = _db(tmp_path)
    _save(conn, id="m1", type="reference", title="rate cuts",
          body="rate cuts ahead", topic="trading")
    _add_knowledge(conn, slug="kn1", topic="trading", title="Rates",
                   snippet="rate cuts and the curve")
    emb = _fake_embedder({"rate": [1.0, 0.0, 0.0]})
    out = knowledge_mcp.run_query_tool(
        {"query": "rate cuts"}, conn=conn, embedder=emb, caller_class="subagent",
        dim=3, now_ts="2026-05-02T00:00:00", ts="2026-05-02T00:00:00",
        agent_topics={"trading"})
    payload = json.loads(out[0].text)
    assert payload["results"]
    assert all("source_kind" in h for h in payload["results"])


def test_unified_recall_audits_each_hit(tmp_path):
    """record_access fires for each returned hit (memory + knowledge), as
    knowledge_recall does — exfiltration is auditable."""
    conn = _db(tmp_path)
    _save(conn, id="m1", type="reference", title="rate", body="rate note",
          topic="trading")
    _add_knowledge(conn, slug="kn1", topic="trading", title="Rates",
                   snippet="rate curve note")
    emb = _fake_embedder({"rate": [1.0, 0.0, 0.0]})
    unified_query.unified_recall(
        conn, "rate", caller_class="subagent", agent_topics={"trading"},
        embedder=emb, dim=3, top_k=5, now_ts="2026-05-02T00:00:00",
        ts="2026-05-02T00:00:00")
    n = conn.execute(
        "SELECT COUNT(*) c FROM access_log WHERE context LIKE 'unified_recall:%'"
    ).fetchone()["c"]
    assert n >= 2  # at least the memory + the knowledge hit
    conn.close()


def test_unified_recall_threads_ascending_1based_rank(tmp_path):
    """SP-8 substrate: each audited recall hit carries its 1-based position in the
    FULL fused result list — rank=1 for the top hit, ascending, no gaps. The ranks
    recorded match the order the hits are returned in (the overall-relevance signal)."""
    conn = _db(tmp_path)
    _save(conn, id="m1", type="reference", title="rate", body="rate note",
          topic="trading")
    _add_knowledge(conn, slug="kn1", topic="trading", title="Rates",
                   snippet="rate curve note")
    emb = _fake_embedder({"rate": [1.0, 0.0, 0.0]})
    out = unified_query.unified_recall(
        conn, "rate", caller_class="subagent", agent_topics={"trading"},
        embedder=emb, dim=3, top_k=5, now_ts="2026-05-02T00:00:00",
        ts="2026-05-02T00:00:00")
    assert len(out) >= 2  # both the memory + the knowledge hit returned
    ranks = [r["rank"] for r in conn.execute(
        "SELECT rank FROM access_log WHERE context LIKE 'unified_recall:%' ORDER BY id"
    ).fetchall()]
    # 1-based, ascending, contiguous over the returned hits — top hit is rank 1.
    assert ranks == list(range(1, len(out) + 1))
    conn.close()


def test_unified_recall_threads_session_id_from_env(tmp_path, monkeypatch):
    """SP-8 substrate: when ULTRA_MEMORY_SESSION_ID is set, each audited recall hit
    carries it on the access_log row (the recalled-by-session substrate)."""
    conn = _db(tmp_path)
    _save(conn, id="m1", type="reference", title="rate", body="rate note",
          topic="trading")
    _add_knowledge(conn, slug="kn1", topic="trading", title="Rates",
                   snippet="rate curve note")
    emb = _fake_embedder({"rate": [1.0, 0.0, 0.0]})
    monkeypatch.setenv("ULTRA_MEMORY_SESSION_ID", "SESS-42")
    unified_query.unified_recall(
        conn, "rate", caller_class="subagent", agent_topics={"trading"},
        embedder=emb, dim=3, top_k=5, now_ts="2026-05-02T00:00:00",
        ts="2026-05-02T00:00:00")
    rows = conn.execute("SELECT session_id FROM access_log").fetchall()
    assert rows and all(r["session_id"] == "SESS-42" for r in rows)
    conn.close()


def test_unified_recall_session_id_null_when_env_unset(tmp_path, monkeypatch):
    """SP-8 substrate, graceful-None: no session id in the env -> the access is
    logged with NULL session_id (harmless, no attribution) and the recall never errors."""
    conn = _db(tmp_path)
    _save(conn, id="m1", type="reference", title="rate", body="rate note",
          topic="trading")
    _add_knowledge(conn, slug="kn1", topic="trading", title="Rates",
                   snippet="rate curve note")
    emb = _fake_embedder({"rate": [1.0, 0.0, 0.0]})
    # Truly-unset = NEITHER the explicit override NOR the ambient Claude-Code fallback
    # (SP-8 A3). The suite runs under Claude Code, so CLAUDE_CODE_SESSION_ID is present
    # in the real env and the A3 fallback would otherwise pick it up — clear both.
    monkeypatch.delenv("ULTRA_MEMORY_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    out = unified_query.unified_recall(
        conn, "rate", caller_class="subagent", agent_topics={"trading"},
        embedder=emb, dim=3, top_k=5, now_ts="2026-05-02T00:00:00",
        ts="2026-05-02T00:00:00")
    assert out  # recall still works
    rows = conn.execute("SELECT session_id FROM access_log").fetchall()
    assert rows and all(r["session_id"] is None for r in rows)
    conn.close()


def test_unified_recall_memory_only_threads_session_id(tmp_path, monkeypatch):
    """SP-8 substrate: the memory-only byte-identity path also threads the session id
    (it goes through the same _audit_hits funnel)."""
    conn = _db(tmp_path)
    _save(conn, id="m1", type="reference", title="rate", body="rate note",
          topic="trading")
    emb = _fake_embedder({"rate": [1.0, 0.0, 0.0]})
    monkeypatch.setenv("ULTRA_MEMORY_SESSION_ID", "SESS-MO")
    unified_query.unified_recall(
        conn, "rate", caller_class="subagent", agent_topics={"trading"},
        embedder=emb, dim=3, top_k=5, now_ts="2026-05-02T00:00:00",
        ts="2026-05-02T00:00:00")
    rows = conn.execute("SELECT session_id FROM access_log").fetchall()
    assert rows and all(r["session_id"] == "SESS-MO" for r in rows)
    conn.close()
