"""SP-3 Stage 6 — `unified_recall` + topic/type access scope (§5.6, D9/D10/D11).

The cross-store WARM retrieval surface: one ranked list spanning the memory store
(`memories`) and the Expert-Knowledge mirror (`unified_index`), scoped by the
orthogonal (type × topic) access wall, fused with the FU-4 best-rank-per-backend
RRF, and weighted by the (inert-until-§7a) `outcome_weight`. No LLM on this path.

──────────────────────────────────────────────────────────────────────────────
DECISION D-S6 — agnostic-vs-parity tension resolution (the auditable why)
──────────────────────────────────────────────────────────────────────────────
Spec §5.6 says unified_recall "reuses the wiki_query backends + FU-4 RRF". But
`scripts/wiki_query.py` is a TRADING-side module, and the top NFR of SP-3 (the
project-agnostic boundary, asserted by `test_engine_has_no_wiki_or_trading_import`)
forbids the engine importing ANYTHING from Trading. We therefore do NOT import
`wiki_query`; we re-implement its *algorithm* engine-side:

  • Memory side    → the engine's own `memory_query.query_memories(include_types=…)`,
                     then filter to `topic ∈ agent_topics OR topic IS NULL`.
                     NOTE (asymmetry, R3 FIX 1): the memory side ranks by
                     embedding-cosine ONLY — it has NO BM25-only fallback, so it
                     REQUIRES a real `embedder`. `embedder=None` raises a clear
                     ValueError on any non-empty store. Only the KNOWLEDGE side
                     (below) degrades to BM25-only when `embedder is None`.
  • Knowledge side → a NEW GENERIC ranker over the `unified_index` rows whose
                     `topic ∈ agent_topics`:
                       (a) a small generic BM25 over each row's text
                           (`title` + full body; NO frontmatter — see
                           `_knowledge_doc_text`);
                       (b) embedding-cosine over the SHARED `embeddings` table with
                           `target_kind='knowledge'` (reusing the same cosine
                           machinery `memory_query`/`retrieval_core` already use —
                           NO new embedder, NO model download).
                     Both are generic IR — zero Trading specifics.
  • Fusion         → a GENERIC re-implementation of wiki_query's FU-4
                     "best-rank-per-backend" rank-based RRF (single credit per item
                     across a backend's rank list; RRF k=60; scale-invariant — fuse
                     RANKS, never raw scores). We replicate the algorithm of
                     `fuse_and_build_results_multi`; we do NOT import it.
  • Each final fused score is multiplied by the item's `outcome_weight`
    (`unified_index` / `memories`; inert default 1.0).
  • Every returned hit is `record_access`-audited, exactly as `knowledge_recall`
    does for `knowledge_query`.

PARITY NOTE (auditable): the cross-store byte-identity to Trading's `wiki_query`
output is NOT achievable here — the engine cannot import that module. Test #3
below is a SELF-golden regression fence over unified_recall's OWN output. True
cross-codebase parity with `wiki_query` is **deferred to a Trading-side SP-5
integration test** (which CAN import both). The memory-store byte-identity (Test
#1) IS enforced here, because that backend is engine-native.
"""
import json
import math
import re

from . import knowledge_mcp, memory_query, retrieval_core
from .redact_secrets import strip_secrets

_RRF_K = 60
_TOKEN_RE = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------------------
# Generic IR primitives (no Trading specifics).
# ---------------------------------------------------------------------------

def _tokenize(text):
    return _TOKEN_RE.findall((text or "").lower())


def _bm25_rank(query, docs, *, k1=1.5, b=0.75):
    """A small GENERIC Okapi BM25 over `docs` (a {id: text} map). Returns
    [(id, score)] ranked by score desc — only the RANKS are used downstream (RRF is
    scale-invariant), so the exact constants are not load-bearing. Self-contained
    (no plugin import) so the engine stays project-agnostic.

    A doc with zero query-term overlap scores 0.0 and is dropped from the ranking
    (it contributes no RRF credit), mirroring wiki_query's BM25 which only returns
    matched pages.
    """
    q_terms = set(_tokenize(query))
    if not q_terms or not docs:
        return []
    tokenized = {doc_id: _tokenize(text) for doc_id, text in docs.items()}
    n_docs = len(tokenized)
    if n_docs == 0:
        return []
    lengths = {doc_id: len(toks) for doc_id, toks in tokenized.items()}
    avgdl = (sum(lengths.values()) / n_docs) if n_docs else 0.0
    # Document frequency per query term.
    df = {}
    for term in q_terms:
        df[term] = sum(1 for toks in tokenized.values() if term in toks)
    scored = []
    for doc_id, toks in tokenized.items():
        if not toks:
            continue
        dl = lengths[doc_id]
        score = 0.0
        for term in q_terms:
            f = toks.count(term)
            if f == 0 or df[term] == 0:
                continue
            # Standard BM25 idf (with the +1 smoothing so it stays non-negative).
            idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
            denom = f + k1 * (1 - b + b * (dl / avgdl if avgdl else 0.0))
            score += idf * (f * (k1 + 1) / denom) if denom else 0.0
        if score > 0.0:
            scored.append((doc_id, score))
    scored.sort(key=lambda t: (-t[1], t[0]))
    return scored


def _rrf_score(ranks, *, k=_RRF_K):
    """Reciprocal Rank Fusion score for a single item across backends.

    `ranks` is a list of 1-indexed positions (None if the item was not found by
    that backend; the term is dropped). Replicates wiki_query.rrf_score's formula
    `sum 1/(k + r)` exactly so the rank-fusion semantics match (FU-4)."""
    return sum(1.0 / (k + r) for r in ranks if r is not None)


def _best_rank_rrf(backends):
    """GENERIC re-implementation of wiki_query's FU-4 best-rank-per-backend RRF.

    `backends` is a list of ranked id-lists (each already ordered best-first within
    its backend). For each item we take its BEST (lowest) rank within EACH backend,
    then score it ONCE: `sum over backends of 1/(k + best_rank)`. Single credit per
    item per backend — a cross-root duplicate at rank-1 in two of one backend's
    lists is NOT double-counted (FU-4 follow-up); a rank-1-in-a-tiny-corpus hit
    still earns the full 1/(k+1) regardless of any other backend's raw score scale
    (scale-invariance — fuse RANKS, never raw scores).

    Returns {id: rrf_total}. We deliberately do NOT import Trading's
    `fuse_and_build_results_multi`; this is its algorithm, engine-side.
    """
    best_rank_per_backend = []  # list parallel to `backends`: {id: best 1-idx rank}
    for ranked in backends:
        best = {}
        for i, item_id in enumerate(ranked):
            rank = i + 1
            if item_id not in best or rank < best[item_id]:
                best[item_id] = rank
        best_rank_per_backend.append(best)
    all_ids = set()
    for best in best_rank_per_backend:
        all_ids.update(best)
    return {
        item_id: _rrf_score([best.get(item_id) for best in best_rank_per_backend])
        for item_id in all_ids
    }


# ---------------------------------------------------------------------------
# Topic access scope (the orthogonal axis onto the type wall, D10).
# ---------------------------------------------------------------------------

def topic_scope_from_env(env, conn=None, *, agent_name=None):
    """Resolve a caller's `agent_topics` set — FAIL-CLOSED, mirroring
    `caller_class_from_env` (knowledge_mcp.py:114).

    Resolution (any source contributes; union of all found):
      • `ULTRA_MEMORY_CALLER_TOPIC` — os.pathsep- or comma-separated topic list.
      • `agent_topic_bindings` rows for `agent_name` (env `ULTRA_MEMORY_AGENT_NAME`
        if not passed) — the persistent many-to-many binding (D10).

    NO binding from EITHER source ⇒ the EMPTY set. An empty topic set means the
    caller sees ONLY `topic IS NULL` operational memories of its allowed types, and
    ZERO topiced knowledge + ZERO topiced memories — the privilege boundary fails
    closed (the degraded mode is SAFE: sees less, never more, Risk §14.5).

    The orchestrator/trusted CLI path does NOT call this — it passes `agent_topics
    is None` (the all-topics sentinel; see `unified_recall`).
    """
    topics = set()
    raw = (env.get("ULTRA_MEMORY_CALLER_TOPIC") or "").strip()
    if raw:
        for part in re.split(r"[,:;]", raw):
            part = part.strip()
            if part:
                topics.add(part)
    if conn is not None:
        name = agent_name or (env.get("ULTRA_MEMORY_AGENT_NAME") or "").strip()
        if name:
            try:
                rows = conn.execute(
                    "SELECT topic FROM agent_topic_bindings WHERE agent_name=?",
                    (name,)).fetchall()
                for r in rows:
                    if r["topic"]:
                        topics.add(r["topic"])
            except Exception:
                # Fail-closed: a binding-lookup error must not WIDEN scope.
                pass
    return topics


# ---------------------------------------------------------------------------
# Knowledge-side candidate ranking over unified_index (generic BM25 + cosine).
# ---------------------------------------------------------------------------

def _row_get(row, key):
    """Read `key` from a sqlite3.Row OR a plain dict, tolerating a column the row
    does not carry (a pre-migration-0005 row has no `bm25_text`). Returns None when
    absent — both sqlite3.Row and dict raise on a missing key, so we guard."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _knowledge_doc_text(row):
    """The generic IR text for a unified_index row: title + FULL body (NO frontmatter).

    SP-6 #6 (D11): the BM25 document is the FULL collapsed body (`bm25_text`), not
    the 400-char display `snippet` — so a query term in a page's back half ranks,
    matching `wiki_query`'s full-text BM25 (closes the SP-5 parity tail-divergence).
    Falls back to `snippet` for un-migrated / NULL `bm25_text` rows (back-compat).

    SP-6 stage-3 parity fix: the page `frontmatter` is DROPPED from the BM25 document.
    The `title` stays (high-signal); the frontmatter `tags:`/`type:` values are noise
    — including them made a query term that matched ONLY a tag value a spurious
    near-zero-relevance tail hit, diverging from Trading's `wiki_query` (which BM25s
    the RENDERED page body, not the frontmatter). BM25ing title + body only aligns the
    engine's document with `wiki_query`'s full-text-body BM25."""
    title = _row_get(row, "title") or ""
    bm25 = _row_get(row, "bm25_text") or _row_get(row, "snippet") or ""
    return f"{title}\n{bm25}"


def _knowledge_candidates(conn, query, *, agent_topics, embedder, dim):
    """Rank `unified_index` rows whose `topic ∈ agent_topics` via two generic
    backends — BM25 and embedding-cosine — and return:
        (bm25_ranked, embed_ranked, by_slug)
    where the two ranked lists are [slug, …] best-first and `by_slug` maps slug →
    its row dict (incl. outcome_weight).

    `agent_topics is None` ⇒ all topics (the orchestrator path). An EMPTY set ⇒ no
    rows (fail-closed: a subagent with no binding sees ZERO topiced knowledge).

    Knowledge has NO NULL-topic rows (every wiki page lives under a topic, §5.6) —
    so a NULL-topic row is never surfaced here regardless of scope.

    FAIL-OPEN on embeddings (the `--extra mcp` env may lack fastembed / vectors):
    if no knowledge vectors exist, the embed backend degrades to an empty list and
    fusion proceeds BM25-only.
    """
    if agent_topics is not None and not agent_topics:
        return [], [], {}
    if agent_topics is None:
        rows = conn.execute(
            "SELECT slug, topic, page_type, title, snippet, bm25_text, frontmatter, "
            "path, outcome_weight FROM unified_index WHERE topic IS NOT NULL"
        ).fetchall()
    else:
        placeholders = ",".join("?" for _ in agent_topics)
        rows = conn.execute(
            "SELECT slug, topic, page_type, title, snippet, bm25_text, frontmatter, "
            f"path, outcome_weight FROM unified_index WHERE topic IN ({placeholders})",
            tuple(agent_topics)).fetchall()
    if not rows:
        return [], [], {}

    by_slug = {r["slug"]: r for r in rows}
    docs = {r["slug"]: _knowledge_doc_text(r) for r in rows}

    # Backend (a): generic BM25.
    bm25_ranked = [slug for slug, _ in _bm25_rank(query, docs)]

    # Backend (b): embedding-cosine over the SHARED embeddings table
    # (target_kind='knowledge'). Reuses retrieval_core's cosine machinery. Vectors
    # are read from cache ONLY here (no embed on the read path for knowledge — they
    # were embedded by wiki_sync at population time); a slug with no cached vector
    # is simply absent from this backend. If the embedder is None OR no slug has a
    # cached vector, this backend is empty -> BM25-only fusion (fail-open).
    embed_ranked = []
    if embedder is not None:
        cached = {}
        for slug in by_slug:
            vrow = conn.execute(
                "SELECT dim, vector FROM embeddings "
                "WHERE target_kind='knowledge' AND target_id=? AND model_name=?",
                (slug, retrieval_core.EMBED_MODEL)).fetchone()
            if vrow is not None and vrow["dim"] == dim:
                cached[slug] = retrieval_core.unpack_vector(vrow["vector"], dim)
        if cached:
            q_vec = embedder([query])[0]
            if len(q_vec) == dim:
                scored = retrieval_core.cosine_search(q_vec, list(cached.items()))
                embed_ranked = [slug for slug, score in scored if score > 0.0]

    return bm25_ranked, embed_ranked, by_slug


# ---------------------------------------------------------------------------
# The warm cross-store recall surface.
# ---------------------------------------------------------------------------

def unified_recall(conn, query, *, caller_class, agent_topics, embedder=None,
                   top_k=5, dim=retrieval_core.EMBED_DIM, now_ts=None, ts=None,
                   audit=True):
    """One ranked list spanning the memory store + the Expert-Knowledge mirror,
    scoped by (type × topic), fused with FU-4 best-rank-per-backend RRF, weighted by
    `outcome_weight` (inert 1.0 until §7a). No LLM. See module docstring (D-S6).

    EMBEDDER (asymmetry, R3 FIX 1): the KNOWLEDGE backend degrades to BM25-only when
    `embedder is None` (fail-open — the `--extra mcp` env may lack fastembed). The
    MEMORY backend does NOT: it ranks by embedding-cosine only and has NO BM25-only
    fallback, so `embedder=None` raises a clear ValueError from `query_memories` for
    ANY non-empty in-scope memory set. The `embedder=None` default is therefore only
    valid for a knowledge-only / empty-memory recall; a real recall over memories
    must pass a real embedder.

    Scope (D10, fail-closed):
      • `allowed_types = allowed_types_for(caller_class)` (the existing type wall).
      • `agent_topics`: a set of topic strings ⇒ the caller is topic-scoped; `None`
        ⇒ ALL topics (the orchestrator / trusted CLI path). The EMPTY set ⇒ a
        subagent with no binding: it sees ONLY `topic IS NULL` operational memories
        of its allowed types, and ZERO topiced knowledge + ZERO topiced memories.

    MEMORY-ONLY BYTE-IDENTITY (§5.6 invariant): when `unified_index` has no rows in
    scope, the knowledge backends are empty, RRF over a single backend (memory) is a
    monotonic re-weighting of `query_memories`' order — and because the memory rows
    carry their full original dict (and outcome_weight defaults 1.0), the returned
    list is byte-identical (same order, same fields) to `query_memories`. The test
    asserts this directly.
    """
    allowed_types = sorted(allowed_types_for_caller(caller_class))

    # Normalize the SCOPED-caller's topic set (R3 bughunt FIX 4): drop None / empty
    # string elements. A None element would otherwise pass `topic=None` to
    # query_memories — which applies NO topic filter and returns EVERY topiced row
    # (the all-topics/orchestrator behavior), a scope WIDENING that violates the
    # "a partial binding sees LESS, never more" privilege invariant; a mixed set like
    # {None,'trading'} would also crash sorted() on the None. After this, a {None}/{''}
    # set collapses to the empty fail-closed set (only NULL-topic rows), and a mixed
    # set scopes to its real topics only. The orchestrator all-topics sentinel
    # (`agent_topics is None`) is untouched — only the set (scoped) case is filtered.
    if agent_topics is not None:
        agent_topics = {t for t in agent_topics if t}

    # --- Memory backend (engine-native; the byte-identity store) ----------------
    # A topic-scoped caller filters to `topic ∈ agent_topics OR topic IS NULL`;
    # query_memories' `topic=` param already keeps NULL rows. It takes ONE topic, so
    # for a multi-topic caller we union per-topic candidate sets (NULL rows dedupe
    # by id). `agent_topics is None` (orchestrator) ⇒ no topic filter at all.
    if agent_topics is None:
        mem_results = memory_query.query_memories(
            conn, query, embedder=embedder, top_k=max(top_k * 4, top_k), dim=dim,
            include_types=allowed_types, now_ts=now_ts)
    elif not agent_topics:
        # Fail-closed: only NULL-topic operational rows of allowed types. We pass a
        # sentinel topic that no row carries; query_memories still returns the
        # `topic IS NULL` rows (its filter is `topic = ? OR topic IS NULL`).
        mem_results = memory_query.query_memories(
            conn, query, embedder=embedder, top_k=max(top_k * 4, top_k), dim=dim,
            include_types=allowed_types, now_ts=now_ts,
            topic="\x00__no_topic_binding__")
    else:
        seen = {}
        for tp in sorted(agent_topics):
            for r in memory_query.query_memories(
                    conn, query, embedder=embedder,
                    top_k=max(top_k * 4, top_k), dim=dim,
                    include_types=allowed_types, now_ts=now_ts, topic=tp):
                # Keep the highest-scoring copy of a row that matched under >1 topic
                # (a NULL-topic row appears in every per-topic query).
                prev = seen.get(r["id"])
                if prev is None or r["score"] > prev["score"]:
                    seen[r["id"]] = r
        mem_results = sorted(seen.values(), key=lambda d: d["score"], reverse=True)

    mem_ranked = [r["id"] for r in mem_results]
    mem_by_id = {r["id"]: r for r in mem_results}
    # outcome_weight for each memory hit (inert 1.0 default; column added in 0004).
    mem_weight = {}
    if mem_ranked:
        placeholders = ",".join("?" for _ in mem_ranked)
        for row in conn.execute(
                f"SELECT id, outcome_weight FROM memories WHERE id IN ({placeholders})",
                tuple(mem_ranked)).fetchall():
            mem_weight[row["id"]] = (
                row["outcome_weight"] if row["outcome_weight"] is not None else 1.0)

    # --- Knowledge backends (generic BM25 + cosine over unified_index) ----------
    bm25_ranked, embed_ranked, kn_by_slug = _knowledge_candidates(
        conn, query, agent_topics=agent_topics, embedder=embedder, dim=dim)

    # MEMORY-ONLY BYTE-IDENTITY (§5.6 invariant, Test #1): when NO knowledge row is
    # in scope (empty unified_index, or a fail-closed empty topic set), the only
    # backend is memory — fusion would be a no-op re-ranking of a single rank-list.
    # We instead return `query_memories`' OWN output dicts, verbatim, truncated to
    # top_k: same order, same fields, same scores — byte-identical. (We still audit,
    # exactly as the knowledge MCP does.) This is the FU-4 single-store fence.
    if not bm25_ranked and not embed_ranked:
        results = []
        for m in mem_results[:top_k]:
            d = dict(m)
            # Read-path redaction (defense-in-depth), mirroring knowledge_recall
            # (knowledge_mcp.py:60): a secret that entered the DB by a path other
            # than the save_memory write chokepoint is still caught here. Byte-
            # identity to query_memories holds for secret-free titles (strip_secrets
            # is a no-op on non-credential text).
            d["title"] = strip_secrets(d.get("title") or "")
            # Extend the type wall to the row's edges (FIX 3): an allowed row's
            # `links` must not leak a forbidden endpoint's id/type to a subagent.
            if "links" in d:
                d["links"] = knowledge_mcp.filter_links_for_caller(
                    conn, d["links"], caller_class=caller_class)
            results.append(d)
        _audit_hits(conn, results, caller_class=caller_class,
                    ts=ts, now_ts=now_ts, audit=audit, memory_only=True)
        return results

    # --- FU-4 best-rank-per-backend RRF across the three backends ---------------
    # Memory ids and knowledge slugs share one id-space here only as namespaced
    # tuples (kind, key) so a memory id never collides with a wiki slug.
    def _mk(kind, key):
        return (kind, key)

    backends = [
        [_mk("memory", mid) for mid in mem_ranked],
        [_mk("knowledge", slug) for slug in bm25_ranked],
        [_mk("knowledge", slug) for slug in embed_ranked],
    ]
    rrf = _best_rank_rrf(backends)

    # --- Multiply by outcome_weight (inert 1.0 until §7a, D9) -------------------
    weighted = {}
    for (kind, key), base in rrf.items():
        if kind == "memory":
            w = mem_weight.get(key, 1.0)
        else:
            row = kn_by_slug.get(key)
            ow = row["outcome_weight"] if row is not None else None
            w = ow if ow is not None else 1.0
        weighted[(kind, key)] = base * w

    # R4 FIX 3: a STABLE secondary key. `_best_rank_rrf` builds the rrf dict by
    # iterating a `set` of (kind,key) tuples — PYTHONHASHSEED-dependent order — so a
    # bare `key=score, reverse=True` reorders ties run-to-run, flipping top_k
    # membership of the one ranked list feeding the gist + MCP. The (kind,key) tuple
    # is unique + orderable, so (-score, key) is a deterministic TOTAL order (same
    # pattern as the BM25 sort at line 109). Determinizes output regardless of
    # set-iteration order; does NOT change WHICH score ranks where.
    ordered = sorted(weighted.items(), key=lambda kv: (-kv[1], kv[0]))

    # --- Build result dicts -----------------------------------------------------
    results = []
    for (kind, key), score in ordered:
        if kind == "memory":
            m = mem_by_id[key]
            results.append({
                "source_kind": "memory",
                "id": m["id"],
                # Read-path redaction (defense-in-depth), mirroring knowledge_recall.
                "title": strip_secrets(m["title"] or ""),
                "type": m["type"],
                "status": m["status"],
                "score": score,
                "stale": m["stale"],
                # Extend the type wall to the row's edges (FIX 3).
                "links": knowledge_mcp.filter_links_for_caller(
                    conn, m["links"], caller_class=caller_class),
            })
        else:
            row = kn_by_slug[key]
            results.append({
                "source_kind": "knowledge",
                "slug": row["slug"],
                "topic": row["topic"],
                # Read-path redaction (defense-in-depth): the documented free-form
                # `Edit` exception lets a secret reach a wiki page; redact it here
                # (and wiki_sync redacts at population), mirroring knowledge_recall.
                "title": strip_secrets(row["title"] or ""),
                "page_type": row["page_type"],
                "snippet": strip_secrets(row["snippet"] or ""),
                "path": row["path"],
                "score": score,
            })
        if len(results) >= top_k:
            break

    _audit_hits(conn, results, caller_class=caller_class, ts=ts, now_ts=now_ts,
                audit=audit, memory_only=False)
    return results


def _audit_hits(conn, results, *, caller_class, ts, now_ts, audit, memory_only):
    """record_access each returned hit (as knowledge_recall does, §5.6). Best-effort:
    an audit-write hiccup must never fail a recall. `memory_only` (the byte-identity
    path) has no `source_kind` key — every hit is a memory id."""
    audit_ts = ts or now_ts
    if not (audit and audit_ts):
        return
    import os

    from . import memory_lib
    # SP-8 substrate (§5.1): thread the GENERIC session id (env, graceful-None) onto
    # every audited recall row, so a later attribution step can ask "which session
    # recalled this unit?". Unset env → NULL → harmless (no attribution), never errors.
    session_id = memory_lib.session_id_from_env(os.environ)
    # SP-8 substrate: `results` is ALREADY in fused-rank order, so its 1-based
    # enumerate position IS the unit's overall-relevance rank (rank=1 = top hit,
    # counting both memory and knowledge hits) — the signal a later top-k
    # attribution policy needs. Recorded only; no behavioral effect here.
    for rank, item in enumerate(results, start=1):
        if memory_only or item.get("source_kind") == "memory":
            tk, tid = "memory", item["id"]
        else:
            tk, tid = "knowledge", item["slug"]
        try:
            memory_lib.record_access(
                conn, target_kind=tk, target_id=tid, ts=audit_ts,
                context=f"unified_recall:{caller_class}", session_id=session_id,
                rank=rank)
        except Exception:
            pass


def allowed_types_for_caller(caller_class):
    """Thin alias so this module reads self-contained; delegates to the canonical
    type wall in knowledge_mcp (single source of truth for the privilege classes)."""
    return knowledge_mcp.allowed_types_for(caller_class)
