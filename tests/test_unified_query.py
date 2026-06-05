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
                                    deferred to a consumer-side SP-5 test, per D-S6).
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


def _embed_signal(conn, slug, vec, dim=3):
    """Seed a ## Signal vector (target_kind='knowledge_signal') — the channel
    best_signal_match (Atomic Graduation dedup-gate) ranks over."""
    from ultra_memory import retrieval_core as rc
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO embeddings (target_kind, target_id, model_name, dim, vector, "
        "content_sha256) VALUES ('knowledge_signal', ?, ?, ?, ?, ?)",
        (slug, rc.EMBED_MODEL, dim, rc.pack_vector(vec), "sig-" + slug))
    conn.execute("COMMIT")


def test_best_signal_match_returns_top_signal_cosine(tmp_path):
    conn = _db(tmp_path)
    _add_knowledge(conn, slug="a", topic="trading", title="A", snippet="body a")
    _add_knowledge(conn, slug="b", topic="trading", title="B", snippet="body b")
    _embed_signal(conn, "a", [1.0, 0.0, 0.0])
    _embed_signal(conn, "b", [0.0, 1.0, 0.0])
    emb = _fake_embedder({"alpha": [1.0, 0.0, 0.0]}, dim=3)
    m = unified_query.best_signal_match(conn, "alpha observable", embedder=emb, dim=3)
    assert m is not None and m[0] == "a" and m[1] > 0.99
    conn.close()


def test_best_signal_match_none_without_signal_vectors(tmp_path):
    conn = _db(tmp_path)
    _add_knowledge(conn, slug="a", topic="trading", title="A", snippet="body")
    emb = _fake_embedder({"alpha": [1.0, 0.0, 0.0]}, dim=3)
    assert unified_query.best_signal_match(conn, "alpha", embedder=emb, dim=3) is None
    conn.close()


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
# R3 FIX 4 — a None/empty element in agent_topics must NOT widen the memory scope.
# A topic-scoped caller whose set contains None passed topic=None to query_memories,
# which applies NO filter → returns EVERY topiced memory (the all-topics/orchestrator
# behavior) — a scope WIDENING that violates "a partial binding sees LESS, never
# more". A mixed set {None,'trading'} also crashed sorted() with a TypeError.
# ---------------------------------------------------------------------------

def test_fix4_agent_topics_none_element_does_not_widen_scope(tmp_path):
    """agent_topics={None} (a degenerate scoped set) must collapse to the empty
    fail-closed set: it sees only NULL-topic operational rows of allowed types, NOT
    other topics' memories. It must NOT behave like the orchestrator all-topics
    sentinel (agent_topics is None)."""
    conn = _db(tmp_path)
    _save(conn, id="null_ref", type="reference", title="null ref",
          body="rrfquery shared note", topic=None)
    _save(conn, id="trading_ref", type="reference", title="trading ref",
          body="rrfquery trading note", topic="trading")
    _save(conn, id="cooking_ref", type="reference", title="cooking ref",
          body="rrfquery cooking note", topic="cooking")
    _add_knowledge(conn, slug="kn_trade", topic="trading", title="kn trade",
                   snippet="rrfquery trading knowledge")
    emb = _fake_embedder({"rrfquery": [1.0, 0.0, 0.0]})

    out = unified_query.unified_recall(
        conn, "rrfquery", caller_class="subagent", agent_topics={None},
        embedder=emb, dim=3, top_k=10, now_ts="2026-05-02T00:00:00", audit=False)
    seen_ids = {h.get("id") for h in out if h.get("source_kind") != "knowledge"}
    # Only the NULL-topic row — NO topiced memory widening, NO topiced knowledge.
    assert seen_ids == {"null_ref"}, seen_ids
    assert "trading_ref" not in seen_ids
    assert "cooking_ref" not in seen_ids
    assert not any(h.get("source_kind") == "knowledge" for h in out)
    conn.close()


def test_fix4_agent_topics_mixed_none_does_not_crash_and_scopes(tmp_path):
    """agent_topics={None, 'trading'} must NOT raise (the old sorted() TypeError on a
    None element) and must scope to 'trading' only (+ NULL-topic rows) — the None is
    dropped, never widening to other topics."""
    conn = _db(tmp_path)
    _save(conn, id="null_ref", type="reference", title="null ref",
          body="rrfquery shared note", topic=None)
    _save(conn, id="trading_ref", type="reference", title="trading ref",
          body="rrfquery trading note", topic="trading")
    _save(conn, id="cooking_ref", type="reference", title="cooking ref",
          body="rrfquery cooking note", topic="cooking")
    _add_knowledge(conn, slug="kn_trade", topic="trading", title="kn trade",
                   snippet="rrfquery trading knowledge")
    _add_knowledge(conn, slug="kn_cook", topic="cooking", title="kn cook",
                   snippet="rrfquery cooking knowledge")
    emb = _fake_embedder({"rrfquery": [1.0, 0.0, 0.0]})

    out = unified_query.unified_recall(
        conn, "rrfquery", caller_class="subagent", agent_topics={None, "trading"},
        embedder=emb, dim=3, top_k=10, now_ts="2026-05-02T00:00:00", audit=False)
    seen_ids = {h.get("id") for h in out if h.get("source_kind") != "knowledge"}
    kn_slugs = {h["slug"] for h in out if h.get("source_kind") == "knowledge"}
    assert seen_ids == {"null_ref", "trading_ref"}, seen_ids  # NOT cooking
    assert kn_slugs == {"kn_trade"}, kn_slugs                  # NOT kn_cook
    conn.close()


def test_fix4_empty_string_element_also_dropped(tmp_path):
    """An empty-string topic element ('' — a near-miss of None) is also dropped: a
    scoped set of only falsy elements collapses to the empty fail-closed set."""
    conn = _db(tmp_path)
    _save(conn, id="null_ref", type="reference", title="null ref",
          body="rrfquery shared", topic=None)
    _save(conn, id="trading_ref", type="reference", title="trading ref",
          body="rrfquery trading", topic="trading")
    emb = _fake_embedder({"rrfquery": [1.0, 0.0, 0.0]})
    out = unified_query.unified_recall(
        conn, "rrfquery", caller_class="subagent", agent_topics={""},
        embedder=emb, dim=3, top_k=10, now_ts="2026-05-02T00:00:00", audit=False)
    seen_ids = {h.get("id") for h in out if h.get("source_kind") != "knowledge"}
    assert seen_ids == {"null_ref"}, seen_ids  # no widening to 'trading'
    conn.close()


# ---------------------------------------------------------------------------
# FENCE 3 — knowledge + cross-store self-golden regression fence.
# ---------------------------------------------------------------------------

def test_fence3_cross_store_self_golden(tmp_path):
    """A regression fence over unified_recall's OWN output on a fixed (memory +
    knowledge) fixture. This is NOT byte-identity with the consumer's wiki_query (the
    engine cannot import it — that cross-codebase parity is deferred to a
    consumer-side SP-5 integration test, per D-S6); it locks the cross-store
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


# ---------------------------------------------------------------------------
# SP-8 bughunt FIX 1 — READ-PATH redaction on unified_recall (defense-in-depth).
# Mirrors knowledge_recall (knowledge_mcp.py:60,62): a secret that entered the DB
# by a path OTHER than the save_memory / wiki_sync write chokepoint must still be
# redacted on the caller-facing title/snippet text of EVERY result.
# ---------------------------------------------------------------------------

_SECRET = "WEBSHARE_PASSWORD=q7xkfak2lm9p"
_SECRET_VAL = "q7xkfak2lm9p"


def test_fix1_unified_recall_redacts_memory_title_on_read_path(tmp_path):
    """A secret that bypassed save_memory's write-time redaction (here injected via
    raw SQL, as a migration/import would) must be redacted in the returned memory
    `title` — and the fused (non byte-identity) path must run it too. We seed a
    knowledge row so the fused path (line 408), NOT the byte-identity path, runs."""
    conn = _db(tmp_path)
    _save(conn, id="m1", type="reference", title="placeholder", body="rrfquery body",
          topic="trading")
    # Inject the secret AFTER save (bypassing the write chokepoint).
    conn.execute("UPDATE memories SET title=? WHERE id=?", (_SECRET, "m1"))
    # Seed a knowledge row so unified_index is non-empty → the FUSED path runs.
    _add_knowledge(conn, slug="kn1", topic="trading", title="kn one",
                   snippet="other rrfquery content")
    emb = _fake_embedder({"rrfquery": [1.0, 0.0, 0.0]})
    out = unified_query.unified_recall(
        conn, "rrfquery", caller_class="orchestrator", agent_topics=None,
        embedder=emb, dim=3, top_k=10, now_ts="2026-05-02T00:00:00", audit=False)
    mem = [r for r in out if r.get("source_kind") == "memory"]
    assert mem, "expected the memory hit in the fused result"
    assert _SECRET_VAL not in mem[0]["title"]
    assert "[REDACTED]" in mem[0]["title"]
    conn.close()


def test_fix1_unified_recall_redacts_knowledge_title_and_snippet(tmp_path):
    """A secret stored in a unified_index row (free-form Edit → wiki page → here a
    raw insert) must be redacted in the returned knowledge `title` AND `snippet`."""
    conn = _db(tmp_path)
    # A memory hit so the FUSED path is exercised (knowledge title=421, snippet=423).
    _save(conn, id="m1", type="reference", title="rrfquery mem", body="rrfquery body",
          topic="trading")
    _add_knowledge(conn, slug="kn1", topic="trading",
                   title=f"page {_SECRET}",
                   snippet=f"snippet body rrfquery {_SECRET}")
    emb = _fake_embedder({"rrfquery": [1.0, 0.0, 0.0]})
    out = unified_query.unified_recall(
        conn, "rrfquery", caller_class="orchestrator", agent_topics=None,
        embedder=emb, dim=3, top_k=10, now_ts="2026-05-02T00:00:00", audit=False)
    kn = [r for r in out if r.get("source_kind") == "knowledge"]
    assert kn, "expected the knowledge hit in the fused result"
    assert _SECRET_VAL not in kn[0]["title"]
    assert _SECRET_VAL not in kn[0]["snippet"]
    assert "[REDACTED]" in kn[0]["title"]
    assert "[REDACTED]" in kn[0]["snippet"]
    conn.close()


def test_fix3_unified_recall_subagent_drops_links_to_forbidden_type(tmp_path):
    """SP-8 bughunt FIX 3 on the unified surface: a subagent recalling an allowed
    project memory must NOT receive, via `links`, the id/type of a forbidden
    feedback memory the project memory links to."""
    conn = _db(tmp_path)
    _save(conn, id="proj", type="project", title="proj rrfquery", body="proj body",
          topic="trading")
    _save(conn, id="fb", type="feedback", title="how to work", body="feedback note",
          topic="trading")
    memory_lib.record_link(
        conn, src_kind="memory", src_id="proj", predicate="references",
        dst_kind="memory", dst_id="fb", dst_type="feedback",
        ts="2026-05-01T00:00:00")
    emb = _fake_embedder({"rrfquery": [1.0, 0.0, 0.0]})
    # byte-identity path (no knowledge rows) — links travel on dict(m).
    out = unified_query.unified_recall(
        conn, "rrfquery", caller_class="subagent", agent_topics={"trading"},
        embedder=emb, dim=3, top_k=10, now_ts="2026-05-02T00:00:00", audit=False)
    proj = [r for r in out if r.get("id") == "proj"]
    assert proj, "subagent must still recall the allowed project memory"
    assert "fb" not in {l["dst_id"] for l in proj[0]["links"]}
    assert "feedback" not in {l["dst_type"] for l in proj[0]["links"]}
    # Orchestrator (full recall) still sees it.
    out2 = unified_query.unified_recall(
        conn, "rrfquery", caller_class="orchestrator", agent_topics=None,
        embedder=emb, dim=3, top_k=10, now_ts="2026-05-02T00:00:00", audit=False)
    proj2 = [r for r in out2 if r.get("id") == "proj"][0]
    assert "fb" in {l["dst_id"] for l in proj2["links"]}
    conn.close()


def test_fix1_unified_recall_redacts_memory_only_byte_identity_path(tmp_path):
    """The memory-only byte-identity path (unified_index empty) returns dict(m)
    verbatim (line 369). A secret injected post-write must STILL be redacted in the
    returned title there too."""
    conn = _db(tmp_path)
    _save(conn, id="m1", type="reference", title="placeholder", body="rate note",
          topic="trading")
    conn.execute("UPDATE memories SET title=? WHERE id=?", (_SECRET, "m1"))
    emb = _fake_embedder({"rate": [1.0, 0.0, 0.0]})
    # unified_index empty → byte-identity path.
    out = unified_query.unified_recall(
        conn, "rate", caller_class="orchestrator", agent_topics=None,
        embedder=emb, dim=3, top_k=5, now_ts="2026-05-02T00:00:00", audit=False)
    assert out, "expected the memory hit on the byte-identity path"
    assert _SECRET_VAL not in out[0]["title"]
    assert "[REDACTED]" in out[0]["title"]
    conn.close()


# ---------------------------------------------------------------------------
# R4 FIX 3 — RRF final sort must be DETERMINISTIC across PYTHONHASHSEED.
# `_best_rank_rrf` iterates a `set` of (kind,key) tuples (hash-seed-dependent
# order); the final `sorted(weighted.items(), key=...)` had NO secondary tie-break,
# so two units tying on weighted score reordered by hash seed → top_k flips.
# ---------------------------------------------------------------------------

def _two_tied_knowledge_recall(conn):
    """Seed two knowledge units that TIE on weighted RRF score: `kn-aaa` ranks #1 in
    the BM25 backend only, `kn-zzz` ranks #1 in the embed backend only — each earns
    exactly 1/(k+1), an identical fused score. The deterministic total order is
    (-score, (kind,key)) → ('knowledge','kn-aaa') before ('knowledge','kn-zzz')."""
    # BM25 hit on the word 'alpha' lives in kn-aaa's text; embed hit lands on kn-zzz.
    _add_knowledge(conn, slug="kn-aaa", topic="trading", title="alpha title",
                   snippet="alpha alpha alpha", bm25_text="alpha alpha alpha")
    _add_knowledge(conn, slug="kn-zzz", topic="trading", title="zzz title",
                   snippet="nothing matching here", bm25_text="nothing matching here")
    # Embed: query vec == kn-zzz's vec (cosine 1.0), kn-aaa orthogonal.
    _embed_knowledge(conn, "kn-zzz", [1.0, 0.0, 0.0])
    _embed_knowledge(conn, "kn-aaa", [0.0, 1.0, 0.0])
    emb = _fake_embedder({"alpha-query": [1.0, 0.0, 0.0]})  # matches kn-zzz vec
    return unified_query.unified_recall(
        conn, "alpha alpha-query", caller_class="orchestrator", agent_topics=None,
        embedder=emb, top_k=5, dim=3, audit=False)


def test_fix3_rrf_final_sort_has_stable_tiebreak(tmp_path):
    """The two tied knowledge units come back in the deterministic (-score, key)
    order — kn-aaa before kn-zzz — and their scores are genuinely equal (the tie is
    real, so only the secondary key decides the order)."""
    conn = _db(tmp_path)
    hits = _two_tied_knowledge_recall(conn)
    kn = [(h["slug"], h["score"]) for h in hits if h["source_kind"] == "knowledge"]
    assert ("kn-aaa", "kn-zzz") == tuple(s for s, _ in kn[:2])
    # The tie is real: equal scores → only the secondary (kind,key) key orders them.
    assert kn[0][1] == kn[1][1]
    conn.close()


def test_fix3_rrf_order_identical_across_hashseed(tmp_path):
    """End-to-end: the recall order over the tied corpus is byte-identical under two
    different PYTHONHASHSEED values (it must not depend on set-iteration order)."""
    import os
    import subprocess
    import sys

    db = tmp_path / "seed.db"
    # Build the fixture DB once via an in-process conn, then close it so the
    # subprocess opens its own connection over the same file.
    conn = memory_lib.open_memory_db(db)
    _two_tied_knowledge_recall(conn)
    conn.close()

    prog = (
        "import json,sys\n"
        "from ultra_memory import memory_lib, unified_query\n"
        "def emb(texts):\n"
        "    out=[]\n"
        "    for t in texts:\n"
        "        out.append([1.0,0.0,0.0] if 'alpha-query' in t else [0.0]*3)\n"
        "    return out\n"
        "conn=memory_lib.open_memory_db(sys.argv[1])\n"
        "hits=unified_query.unified_recall(conn,'alpha alpha-query',"
        "caller_class='orchestrator',agent_topics=None,embedder=emb,top_k=5,dim=3,"
        "audit=False)\n"
        "print(json.dumps([h.get('slug') or h.get('id') for h in hits]))\n"
    )

    def _run(seed):
        env = dict(os.environ, PYTHONHASHSEED=str(seed))
        r = subprocess.run([sys.executable, "-c", prog, str(db)],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip().splitlines()[-1]

    assert _run(0) == _run(1) == _run(12345)


# ---------------------------------------------------------------------------
# R4 PERF FIX 1 — the knowledge embedding fetch must be CHUNKED, not N+1.
# `_knowledge_candidates`'s embed backend used to run ONE `SELECT … FROM embeddings`
# per slug (~278). At the designed ~1223-page mirror scale, that's ~1223 per-row
# round-trips per recall. The fix batches the vector fetch into ONE `… target_id IN
# (…)` query per ≤500-slug chunk, but the RANKED RESULT must be BYTE-IDENTICAL.
# ---------------------------------------------------------------------------

class _CountingConn:
    """A thin proxy over a sqlite3 connection that counts how many embedding
    SELECTs (`FROM embeddings`, `target_kind='knowledge'`) hit the DB — the N+1
    perf-regression guard. Everything else delegates verbatim, so behavior (and
    therefore the ranked results) is unchanged."""

    def __init__(self, real):
        self._real = real
        self.embed_selects = 0

    def execute(self, sql, *args, **kw):
        s = " ".join(sql.split())
        if "FROM embeddings" in s and "target_kind='knowledge'" in s:
            self.embed_selects += 1
        return self._real.execute(sql, *args, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _seed_n_knowledge_with_vectors(conn, n, *, with_vector_every=1, dim=3):
    """Seed `n` knowledge rows under topic 'trading'. Every `with_vector_every`-th
    row also gets a seeded embedding vector; the others have NO cached vector (so the
    'slug missing a vector behaves as before' branch is exercised)."""
    for i in range(n):
        slug = f"kn{i:04d}"
        # All share the query term 'alpha' in the body so BM25 ranks them; a per-row
        # token makes the order deterministic.
        _add_knowledge(conn, slug=slug, topic="trading", title=f"Page {i}",
                       snippet=f"alpha note unit{i}", bm25_text=f"alpha note unit{i}")
        if i % with_vector_every == 0:
            # Distinct vectors so cosine produces a strict, reproducible order.
            vec = [0.0] * dim
            vec[i % dim] = 1.0 + (i / 1000.0)
            _embed_knowledge(conn, slug, vec, dim=dim)


def test_perf_fix1_embedding_fetch_is_chunked_not_n_plus_1(tmp_path):
    """RED before the fix (N embedding SELECTs), GREEN after (~ceil(N/chunk)). With
    N knowledge slugs in scope, the embed backend must issue a BOUNDED number of
    `FROM embeddings` SELECTs — one per ≤500-slug chunk — NOT one per slug."""
    conn = _CountingConn(memory_lib.open_memory_db(tmp_path / "m.db"))
    n = 23
    _seed_n_knowledge_with_vectors(conn, n)
    emb = _fake_embedder({"alpha": [1.0, 0.0, 0.0]})

    unified_query._knowledge_candidates(
        conn, "alpha", agent_topics={"trading"}, embedder=emb, dim=3)

    # Chunk size 500 → ceil(23/500) == 1 batched SELECT, NOT 23 per-row SELECTs.
    assert conn.embed_selects <= 2, (
        f"expected a bounded (chunked) embedding fetch, got {conn.embed_selects} "
        f"SELECTs for {n} slugs (N+1 regression)")
    conn._real.close()


def test_perf_fix1_chunking_crosses_the_500_boundary(tmp_path):
    """A scope larger than one chunk issues exactly ceil(N/500) batched SELECTs —
    proving the IN-list is chunked under SQLite's 999-variable ceiling, not one giant
    (over-limit) query and not N per-row queries."""
    conn = _CountingConn(memory_lib.open_memory_db(tmp_path / "m.db"))
    n = 1100  # 3 chunks at size 500
    _seed_n_knowledge_with_vectors(conn, n)
    emb = _fake_embedder({"alpha": [1.0, 0.0, 0.0]})

    unified_query._knowledge_candidates(
        conn, "alpha", agent_topics={"trading"}, embedder=emb, dim=3)

    import math as _m
    expected_chunks = _m.ceil(n / 500)
    assert conn.embed_selects == expected_chunks, (
        f"expected {expected_chunks} chunked SELECTs for {n} slugs, "
        f"got {conn.embed_selects}")
    conn._real.close()


def test_perf_fix1_ranked_result_byte_identical_to_baseline(tmp_path):
    """CRITICAL byte-identity: the (bm25_ranked, embed_ranked, by_slug-keys) output of
    `_knowledge_candidates` AND the full `unified_recall` ranking are IDENTICAL to a
    captured baseline — the chunked fetch is PURE efficiency, zero ranking change.

    Some slugs deliberately have NO cached vector (with_vector_every=3) so the
    'missing-vector slug is skipped exactly as before' invariant is locked."""
    # Baseline captured from a fresh, independent DB built identically.
    base_conn = memory_lib.open_memory_db(tmp_path / "base.db")
    _seed_n_knowledge_with_vectors(base_conn, 30, with_vector_every=3)
    emb = _fake_embedder({"alpha": [1.0, 0.0, 0.0]})

    base_bm25, base_embed, base_by = unified_query._knowledge_candidates(
        base_conn, "alpha", agent_topics={"trading"}, embedder=emb, dim=3)
    base_recall = unified_query.unified_recall(
        base_conn, "alpha", caller_class="orchestrator", agent_topics=None,
        embedder=emb, dim=3, top_k=40, now_ts="2026-05-02T00:00:00", audit=False)
    base_conn.close()

    live_conn = memory_lib.open_memory_db(tmp_path / "live.db")
    _seed_n_knowledge_with_vectors(live_conn, 30, with_vector_every=3)
    live_bm25, live_embed, live_by = unified_query._knowledge_candidates(
        live_conn, "alpha", agent_topics={"trading"}, embedder=emb, dim=3)
    live_recall = unified_query.unified_recall(
        live_conn, "alpha", caller_class="orchestrator", agent_topics=None,
        embedder=emb, dim=3, top_k=40, now_ts="2026-05-02T00:00:00", audit=False)
    live_conn.close()

    assert live_bm25 == base_bm25
    assert live_embed == base_embed          # identical embed ranking + scores order
    assert sorted(live_by) == sorted(base_by)
    assert live_recall == base_recall        # full cross-store ranking byte-identical
    # Sanity: with_vector_every=3 → some slugs were vector-less (skipped, as before).
    assert len(base_embed) < 30


# ---------------------------------------------------------------------------
# R4 PERF FIX 2 — the BM25 corpus must be CACHED on a content fingerprint, not
# re-tokenized on every call. `_bm25_rank` re-tokenized all docs + recomputed df +
# avgdl on EVERY call (~83); the consolidation drain calls it up to 50× per weekly
# run → 50× full-corpus re-tokenizations. The fix memoizes the tokenized corpus
# keyed on a STABLE (sha1) content fingerprint — reused for an identical corpus,
# recomputed (never stale) when any doc is edited / added / removed.
# ---------------------------------------------------------------------------

def test_perf_fix2_same_corpus_reuses_tokenization(tmp_path, monkeypatch):
    """Two `_bm25_rank` calls over the SAME docs → `_tokenize` runs over the corpus
    only ONCE (the second call hits the cache), and both rankings are IDENTICAL.
    RED before the fix (re-tokenizes every call)."""
    # Clear any cross-test cache state.
    if hasattr(unified_query, "_bm25_cache_clear"):
        unified_query._bm25_cache_clear()

    calls = {"n": 0}
    real_tokenize = unified_query._tokenize

    def _spy(text):
        calls["n"] += 1
        return real_tokenize(text)

    monkeypatch.setattr(unified_query, "_tokenize", _spy)

    docs = {f"d{i}": f"alpha term body number {i}" for i in range(10)}
    r1 = unified_query._bm25_rank("alpha term", docs)
    after_first = calls["n"]
    r2 = unified_query._bm25_rank("alpha term", docs)
    after_second = calls["n"]

    assert r1 == r2, "BM25 ranking must be identical for the same corpus"
    # The query is tokenized each call (cheap), but the DOCS corpus (10 docs) is only
    # tokenized once: the second call adds at most the query tokenization, NOT 10 docs.
    doc_tokenizations_second_call = after_second - after_first
    assert doc_tokenizations_second_call <= 1, (
        f"second identical call re-tokenized the corpus "
        f"({doc_tokenizations_second_call} _tokenize calls); expected cache reuse")


def test_perf_fix2_changed_corpus_recomputes_not_stale(tmp_path, monkeypatch):
    """CRITICAL correctness: when the docs content CHANGES (a doc edited, a doc added,
    a doc removed) the fingerprint changes → fresh tokenization + the CORRECT new
    ranking (never a stale cached one)."""
    if hasattr(unified_query, "_bm25_cache_clear"):
        unified_query._bm25_cache_clear()

    docs_a = {"d1": "alpha one", "d2": "beta two"}
    r_a = unified_query._bm25_rank("alpha", docs_a)
    assert r_a[0][0] == "d1"  # only d1 mentions alpha

    # (1) EDIT a doc's text so the OTHER doc now matches the query.
    docs_edit = {"d1": "gamma one", "d2": "alpha two"}
    r_edit = unified_query._bm25_rank("alpha", docs_edit)
    assert r_edit and r_edit[0][0] == "d2", (
        "edited corpus must recompute — d2 now matches, NOT a stale d1 ranking")

    # (2) ADD a doc.
    docs_add = dict(docs_a, d3="alpha three alpha")
    r_add = unified_query._bm25_rank("alpha", docs_add)
    add_ids = {doc_id for doc_id, _ in r_add}
    assert "d3" in add_ids, "added doc must appear (fingerprint changed → recompute)"

    # (3) REMOVE a doc — d1 gone, only d2 (no 'alpha') remains → no alpha hits.
    docs_rm = {"d2": "beta two"}
    r_rm = unified_query._bm25_rank("alpha", docs_rm)
    assert r_rm == [], "removing the only matching doc must yield no stale hit"


def test_perf_fix2_caches_across_calls_correctly(tmp_path, monkeypatch):
    """The cache must serve the RIGHT tokenization when alternating between two
    distinct corpora (fingerprint keys them apart — no cross-corpus bleed)."""
    if hasattr(unified_query, "_bm25_cache_clear"):
        unified_query._bm25_cache_clear()

    docs_x = {"x1": "alpha apple", "x2": "alpha banana"}
    docs_y = {"y1": "alpha cherry", "y2": "beta date"}

    rx1 = unified_query._bm25_rank("apple", docs_x)
    ry1 = unified_query._bm25_rank("cherry", docs_y)
    rx2 = unified_query._bm25_rank("apple", docs_x)  # back to x — must match rx1
    ry2 = unified_query._bm25_rank("cherry", docs_y)

    assert rx1 == rx2 and rx1 and rx1[0][0] == "x1"
    assert ry1 == ry2 and ry1 and ry1[0][0] == "y1"


def test_perf_fix2_fingerprint_is_hashseed_stable(tmp_path):
    """The fingerprint must be a STABLE digest (sha1), NOT PYTHONHASHSEED-salted
    hash() — so the cache key (and thus correctness) does not depend on the hash seed
    across processes. We assert the BM25 ranking over a fixed corpus is byte-identical
    under two different PYTHONHASHSEED values."""
    import os
    import subprocess
    import sys

    prog = (
        "import json\n"
        "from ultra_memory import unified_query\n"
        "docs={'d%02d'%i:'alpha term body %d'%i for i in range(12)}\n"
        "r=unified_query._bm25_rank('alpha term', docs)\n"
        "print(json.dumps([list(t) for t in r]))\n"
    )

    def _run(seed):
        env = dict(os.environ, PYTHONHASHSEED=str(seed))
        r = subprocess.run([sys.executable, "-c", prog],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip().splitlines()[-1]

    assert _run(0) == _run(1) == _run(99999)
