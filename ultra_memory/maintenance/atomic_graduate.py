"""Atomic Graduation (Recall-Reflex 5.2) — the autonomous capture-findably backstop.

A fenced maintenance beat: drain `atomic_candidate` markers (captured by `session_ingest`)
and turn each durable lesson into a `## Signal`-keyed wiki atomic via the consumer gateway.

Posture (feedback_ship_autonomous_no_dead_flags): ships ON by default; kill-switch
`ATOMIC_GRADUATE_DISABLE` (never an enable-flag). The apply is DETERMINISTIC — the lesson +
observable already came from `session_ingest`'s single OAuth call; this beat makes no LLM call.

Intrinsic safety wall: a THREE-way `## Signal` dedup-gate (merge / skip-grey / create), a
blast-radius cap, create-only (archive-never-delete), `created_by='background_review'`, and
per-candidate fail-open. (Task 5 adds the eval-gate: findable-or-quarantine.)
"""
import hashlib
import re
from pathlib import Path

from ultra_memory.maintenance.session_ingest import (
    pending_atomic_candidates, resolve_atomic_candidate)

DEFAULT_CAP = 3
# Signal-channel merge band, CALIBRATED from the 2026-06-05 live pilot: same-incident
# paraphrased observables cluster at ~0.855 cosine (e.g. the fastembed gotcha captured by
# 9 independent sessions). The wiki's Mechanism-block band uses 0.86, but for the `##
# Signal` channel that leaves same-incident paraphrases stuck in the grey zone (perpetual
# SKIP). 0.84 merges them (distinct incidents score far lower → no false merge). Env-tunable
# via ATOMIC_GRADUATE_DEDUP_UPPER / _LOWER as the channel populates.
DEFAULT_DEDUP_UPPER = 0.84
DEFAULT_DEDUP_LOWER = 0.78
DEFAULT_EVAL_TOP_N = 5
# Inter-candidate (cluster) grouping threshold, CALIBRATED from the 2026-06-05 live pilot
# (78 candidate signals). Greedy union-find over candidate + existing-page `## Signal`
# vectors at 0.80 cosine maps cleanly to real incidents without merging distinct ones (0.70
# over-groups; 0.84 fragments same-incident paraphrases). It is intentionally BELOW the
# page-merge `dedup_upper` (0.84): grouping a candidate INTO an existing page is additive/
# safe, so a slightly looser group is low-risk, whereas creating a page is higher-stakes.
# Env-tunable via ATOMIC_GRADUATE_CLUSTER_COS (read in beat() via _float_env).
DEFAULT_CLUSTER_COS = 0.80
_SOURCE_LABEL = "atomic-graduate"


def _enabled(env) -> bool:
    """Default ON; disabled only when `ATOMIC_GRADUATE_DISABLE` is set (the kill-switch)."""
    return not (env.get("ATOMIC_GRADUATE_DISABLE") or "").strip()


def _slugify(title: str, signal: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:60] or "atomic"
    h = hashlib.sha1((title + "\x00" + signal).encode("utf-8")).hexdigest()[:4]
    return f"{base}-{h}"


def _theme_for(cand, theme_map=None) -> str:
    """The theme-index a candidate registers under. Consumer-supplied via `theme_map`
    (kind -> theme, from config); falls back to the candidate's own `kind` so the engine
    stays domain-agnostic (no consumer theme literals baked in)."""
    return (theme_map or {}).get(cand.get("kind")) or (cand.get("kind") or "general")


def _render_atomic(cand, *, slug, ts, theme_map=None) -> str:
    """The atomic markdown: frontmatter + body + a `## Signal` section. A non-gotcha
    lesson carries an unvalidated confidence label + the entry-trigger disambiguation so a
    downstream consumer never treats an auto-created lesson as established."""
    theme = _theme_for(cand, theme_map)
    kind = cand.get("kind")
    tags = f"[auto-graduated, {kind}]"
    conf = ("\n\nConfidence: [Recent-Regime] — auto-graduated + UNVALIDATED; not an "
            "established rule. (The `## Signal` below is the RECALL trigger, NOT a "
            "strategy entry-trigger.)" if kind == "lesson" else "")
    date = str(ts)[:10]
    fm = (f"---\ntype: mechanism\ntitle: {cand['title']}\ntags: {tags}\n"
          f"created: {date}\nupdated: {date}\ntheme: {theme}\nanchor: {slug}\n"
          f"created_by: background_review\n---\n")
    body = (f"\n# {cand['title']}\n\n{cand['body']}{conf}\n\n"
            f"## Signal\n\n{cand['signal']}\n\n"
            f"Sources: auto-graduated by atomic_graduate from session "
            f"{cand.get('session_id', '?')} ({date}).\n")
    return fm + body


def _page_path_for_slug(conn, slug):
    """The stored `unified_index.path` for `slug` (the merge target), or the bare slug
    when the row is absent (fail-soft — the gateway resolves what it can)."""
    row = conn.execute("SELECT path FROM unified_index WHERE slug=?", (slug,)).fetchone()
    return row["path"] if row else slug


def _normalize_topic(cand, *, valid_topics, default_topic) -> str:
    """Normalize a candidate's topic against the known topics: the extraction may put a
    THEME (not a topic) in the topic field, which would otherwise create a spurious
    top-level topic tree. Unknown → the consumer's default topic."""
    raw = cand.get("topic") or default_topic
    return raw if (valid_topics is None or raw in valid_topics) else default_topic


def _cosine(a, b) -> float:
    """Cosine of two equal-length float vectors (0.0 on a zero / length-mismatch). A thin
    local alias over retrieval_core.cosine so the cluster math reuses the canonical impl."""
    from ultra_memory import retrieval_core
    return retrieval_core.cosine(a, b)


def _load_seed_vectors(conn, topic, *, dim):
    """The existing on-disk `## Signal` (`knowledge_signal`) seed vectors for `topic`:
    {slug: vector}. These seed the clustering so a fresh candidate that paraphrases an
    ALREADY-graduated incident clusters with that page (→ merge, not a near-dup create).
    Fail-soft → {} (no seeds just means every cluster is seedless this run)."""
    from ultra_memory import retrieval_core
    out = {}
    try:
        rows = conn.execute(
            "SELECT e.target_id, e.dim, e.vector FROM embeddings e "
            "JOIN unified_index u ON u.slug = e.target_id "
            "WHERE e.target_kind='knowledge_signal' AND e.model_name=? AND u.topic=?",
            (retrieval_core.EMBED_MODEL, topic)).fetchall()
    except Exception:
        return out
    for r in rows:
        if r["dim"] == dim:
            try:
                out[r["target_id"]] = retrieval_core.unpack_vector(r["vector"], dim)
            except Exception:
                continue
    return out


def _cluster(cand_vecs, seed_vecs, *, cluster_cos):
    """Greedy union-find over candidate + seed signal vectors at `cluster_cos`.

    `cand_vecs` = list of (cand_index, vector); `seed_vecs` = {slug: vector}. Returns a
    list of clusters, each {"cands": [cand_index, …], "seeds": [slug, …]}. A node joins
    another's cluster when their cosine ≥ `cluster_cos` (single-link greedy). Distinct
    incidents (cosine below the threshold) stay in separate clusters."""
    # Nodes: candidates as ("c", idx), seeds as ("s", slug). Union-find over all.
    nodes = [("c", i, v) for i, v in cand_vecs] + [("s", s, v) for s, v in seed_vecs.items()]
    parent = {n[:2]: n[:2] for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            if _cosine(nodes[i][2], nodes[j][2]) >= cluster_cos:
                union(nodes[i][:2], nodes[j][:2])

    groups = {}
    for kind, key, _v in nodes:
        root = find((kind, key))
        g = groups.setdefault(root, {"cands": [], "seeds": []})
        (g["cands"] if kind == "c" else g["seeds"]).append(key)
    return list(groups.values())


def _nearest_seed(cand_idxs, seed_slugs, *, cand_vecs_by_idx, seed_vecs):
    """The (slug, cosine) of the seed nearest to ANY candidate in the cluster — the merge
    target. Picks the single highest candidate↔seed cosine across the cluster."""
    best = (None, -1.0)
    for slug in seed_slugs:
        sv = seed_vecs[slug]
        for ci in cand_idxs:
            cos = _cosine(cand_vecs_by_idx[ci], sv)
            if cos > best[1]:
                best = (slug, cos)
    return best


def _resolve_create(conn, rep, others, *, ts, gateway_run, wiki_root, topic,
                    recall_fn, index_fn, quarantine_fn, embedder, eval_top_n,
                    theme_map, log):
    """Run the create→register→eval-gate flow for ONE representative candidate `rep`,
    then resolve the cluster's `others` as clustered. Returns (created, quarantined,
    clustered) on success; raises on a gateway/eval error so the CALLER can fail-open
    the whole cluster (leaving every candidate unresolved → retry)."""
    slug = _slugify(rep["title"], rep["signal"])
    path = str(Path(wiki_root) / topic / "concepts" / f"{slug}.md")
    gateway_run("create-page",
                ["--path", path, "--topic", topic, "--source-label", _SOURCE_LABEL],
                _render_atomic(rep, slug=slug, ts=ts, theme_map=theme_map))
    gateway_run("register-index",
                ["--slug", slug, "--theme", _theme_for(rep, theme_map),
                 "--summary", rep["title"][:160], "--topic", topic,
                 "--source-label", _SOURCE_LABEL], None)
    quarantined = 0
    # EVAL-GATE: an atomic that is not recall-findable by its OWN observable is useless →
    # quarantine (archive-never-delete), never kept silently. Index the new page inline
    # first (index_fn) so the real recall can reach it this run.
    if recall_fn is not None:
        if index_fn is not None:
            index_fn(conn=conn, slug=slug, topic=topic, title=rep["title"],
                     signal=rep["signal"], body=rep["body"], embedder=embedder)
        hit_slugs = {(h.get("slug") if isinstance(h, dict) else h)
                     for h in (recall_fn(rep["signal"], top_k=eval_top_n) or [])}
        if slug not in hit_slugs:
            if quarantine_fn is not None:
                quarantine_fn(path)
            quarantined = 1
            log(f"atomic_graduate: {rep['title']!r} not recall-findable by its "
                f"signal after create → quarantined")
    # Resolve the whole cluster: the representative + the others (clustered into it).
    resolve_atomic_candidate(conn, event_id=rep["event_id"])
    clustered = 0
    for o in others:
        resolve_atomic_candidate(conn, event_id=o["event_id"])
        clustered += 1
    return 1, quarantined, clustered


def run_atomic_graduate_pass(conn, *, ts, env, gateway_run, signal_match, wiki_root,
                             recall_fn=None, index_fn=None, quarantine_fn=None,
                             embedder=None, cap=DEFAULT_CAP,
                             dedup_upper=DEFAULT_DEDUP_UPPER, dedup_lower=DEFAULT_DEDUP_LOWER,
                             cluster_cos=DEFAULT_CLUSTER_COS,
                             eval_top_n=DEFAULT_EVAL_TOP_N, valid_topics=None,
                             default_topic="default", theme_map=None,
                             log=lambda _m: None) -> dict:
    """Drain `atomic_candidate` markers → CLUSTER candidates + existing-page `## Signal`
    seeds, then per cluster MERGE (≥1 seed) or CREATE ONE (no seed) `## Signal`-keyed
    atomic, capped + fenced + fail-open. `gateway_run(verb, args:list[str],
    content:str|None)` is injected (production: shells the consumer gateway; test: a
    recorder).

    The unit of work is a CLUSTER (greedy union-find at `cluster_cos`, default 0.80) so
    candidate-vs-candidate AND candidate-vs-existing-page near-dups resolve once — the old
    perpetual grey-zone SKIP disappears. With NO embedder (clustering needs vectors) the
    pass FALLS OPEN to the legacy per-candidate three-way dedup-gate, unchanged."""
    if not _enabled(env):
        return {"mode": "disabled", "created": 0, "merged": 0, "skipped": 0,
                "quarantined": 0, "clustered": 0}
    pend = pending_atomic_candidates(conn, limit=max(cap * 10, 50))

    # Embed the candidate signals up front. No embedder, or an embedder that raises, →
    # fall open to the legacy per-candidate path (clustering needs vectors).
    cand_vecs = None
    if embedder is not None:
        try:
            from ultra_memory import retrieval_core
            dim = retrieval_core.EMBED_DIM
            sigs = [c["signal"] for c in pend]
            cand_vecs = embedder(sigs) if sigs else []
        except Exception as exc:
            log(f"atomic_graduate: embedder failed ({exc!r}) — falling open to the "
                f"per-candidate path")
            cand_vecs = None
    if cand_vecs is None:
        return _run_per_candidate_pass(
            conn, pend=pend, ts=ts, gateway_run=gateway_run, signal_match=signal_match,
            wiki_root=wiki_root, recall_fn=recall_fn, index_fn=index_fn,
            quarantine_fn=quarantine_fn, embedder=embedder, cap=cap,
            dedup_upper=dedup_upper, dedup_lower=dedup_lower, eval_top_n=eval_top_n,
            valid_topics=valid_topics, default_topic=default_topic, theme_map=theme_map,
            log=log)

    created = merged = quarantined = clustered = 0

    # Cluster PER TOPIC (preserving the dedup-gate's topic scope: a candidate only
    # clusters with seeds + candidates of its OWN normalized topic).
    by_topic = {}
    for i, c in enumerate(pend):
        topic = _normalize_topic(c, valid_topics=valid_topics, default_topic=default_topic)
        by_topic.setdefault(topic, []).append(i)

    for topic, idxs in by_topic.items():
        seed_vecs = _load_seed_vectors(conn, topic, dim=dim)
        cand_vecs_by_idx = {i: cand_vecs[i] for i in idxs}
        cand_pairs = [(i, cand_vecs[i]) for i in idxs]
        try:
            groups = _cluster(cand_pairs, seed_vecs, cluster_cos=cluster_cos)
        except Exception as exc:   # clustering itself failing must not wedge the topic
            log(f"atomic_graduate: clustering failed for topic {topic!r}: {exc!r}")
            continue
        # Order: process seed (MERGE) clusters first (uncapped, additive), then seedless
        # CREATE clusters (capped). Deterministic by representative for repeatability.
        seed_groups = [g for g in groups if g["seeds"]]
        create_groups = [g for g in groups if not g["seeds"]]
        for g in seed_groups:
            cand_idxs = g["cands"]
            if not cand_idxs:
                continue   # a pure-seed cluster (no candidate this run) — nothing to do
            try:
                slug, _cos = _nearest_seed(
                    cand_idxs, g["seeds"], cand_vecs_by_idx=cand_vecs_by_idx,
                    seed_vecs=seed_vecs)
                gateway_run(
                    "append-validation-log",
                    ["--page", _page_path_for_slug(conn, slug), "--topic", topic,
                     "--source-label", _SOURCE_LABEL],
                    f"- {str(ts)[:10]} [atomic_graduate] **MERGE** — recurring "
                    f"observable (n={len(cand_idxs)}): "
                    f"{pend[cand_idxs[0]]['signal'][:160]}.")
                for ci in cand_idxs:
                    resolve_atomic_candidate(conn, event_id=pend[ci]["event_id"])
                merged += 1
            except Exception as exc:   # per-cluster fail-open: leave unresolved → retry
                log(f"atomic_graduate MERGE failed for topic {topic!r}: {exc!r} — "
                    f"retry next run")
        for g in create_groups:
            if created >= cap:
                log(f"atomic_graduate: blast-radius cap {cap} reached — remaining "
                    f"no-seed cluster(s) left for next run")
                break
            cand_idxs = g["cands"]
            if not cand_idxs:
                continue
            # Representative = the candidate with the LONGEST body (most context).
            rep_idx = max(cand_idxs, key=lambda i: len(pend[i].get("body") or ""))
            rep = pend[rep_idx]
            others = [pend[i] for i in cand_idxs if i != rep_idx]
            try:
                cr, qn, cl = _resolve_create(
                    conn, rep, others, ts=ts, gateway_run=gateway_run,
                    wiki_root=wiki_root, topic=topic, recall_fn=recall_fn,
                    index_fn=index_fn, quarantine_fn=quarantine_fn, embedder=embedder,
                    eval_top_n=eval_top_n, theme_map=theme_map, log=log)
                created += cr
                quarantined += qn
                clustered += cl
            except Exception as exc:   # per-cluster fail-open: whole cluster unresolved
                log(f"atomic_graduate CREATE failed for {rep.get('title')!r}: {exc!r} "
                    f"— retry next run")

    return {"mode": "ran", "created": created, "merged": merged, "skipped": 0,
            "quarantined": quarantined, "clustered": clustered}


def _run_per_candidate_pass(conn, *, pend, ts, gateway_run, signal_match, wiki_root,
                            recall_fn, index_fn, quarantine_fn, embedder, cap,
                            dedup_upper, dedup_lower, eval_top_n, valid_topics,
                            default_topic, theme_map, log) -> dict:
    """The LEGACY per-candidate three-way dedup-gate (merge / skip-grey / create), the
    embedder-None / embedder-raises fail-open path (clustering needs vectors). Behavior
    is unchanged from the pre-cluster engine; kept so a deployment without fastembed still
    drains correctly. Returns the same shape (clustered=0 on this path)."""
    created = merged = skipped = quarantined = 0
    for c in pend:
        if created >= cap:
            log(f"atomic_graduate: blast-radius cap {cap} reached — "
                f"{len(pend) - cap} candidate(s) left for next run")
            break
        topic = _normalize_topic(c, valid_topics=valid_topics, default_topic=default_topic)
        try:
            m = signal_match(conn, c["signal"], embedder=embedder, topic=topic)
            cos = m[1] if m else 0.0
            if m and cos >= dedup_upper:
                # MERGE — reinforce the existing page; never a duplicate.
                gateway_run(
                    "append-validation-log",
                    ["--page", _page_path_for_slug(conn, m[0]), "--topic", topic,
                     "--source-label", _SOURCE_LABEL],
                    f"- {str(ts)[:10]} [atomic_graduate] (n=1): **MERGE** — recurring "
                    f"observable: {c['signal'][:160]}.")
                resolve_atomic_candidate(conn, event_id=c["event_id"])
                merged += 1
                continue
            if m and dedup_lower <= cos < dedup_upper:
                # SKIP-conservative — uncertain; neither a maybe-dup nor a forced merge.
                log(f"atomic_graduate: grey-zone ({cos:.3f}) for {c['title']!r} vs "
                    f"[[{m[0]}]] — skip, retry next run")
                skipped += 1
                continue
            # NOVEL → create a `## Signal`-keyed atomic.
            slug = _slugify(c["title"], c["signal"])
            path = str(Path(wiki_root) / topic / "concepts" / f"{slug}.md")
            gateway_run("create-page",
                        ["--path", path, "--topic", topic, "--source-label", _SOURCE_LABEL],
                        _render_atomic(c, slug=slug, ts=ts, theme_map=theme_map))
            gateway_run("register-index",
                        ["--slug", slug, "--theme", _theme_for(c, theme_map),
                         "--summary", c["title"][:160], "--topic", topic,
                         "--source-label", _SOURCE_LABEL], None)
            created += 1
            # EVAL-GATE: an atomic that is not recall-findable by its OWN observable is
            # useless → quarantine (archive-never-delete), never kept silently. Index the
            # new page inline first (index_fn) so the real recall can reach it this run.
            if recall_fn is not None:
                if index_fn is not None:
                    index_fn(conn=conn, slug=slug, topic=topic, title=c["title"],
                             signal=c["signal"], body=c["body"], embedder=embedder)
                hit_slugs = {(h.get("slug") if isinstance(h, dict) else h)
                             for h in (recall_fn(c["signal"], top_k=eval_top_n) or [])}
                if slug not in hit_slugs:
                    if quarantine_fn is not None:
                        quarantine_fn(path)
                    quarantined += 1
                    log(f"atomic_graduate: {c['title']!r} not recall-findable by its "
                        f"signal after create → quarantined")
            resolve_atomic_candidate(conn, event_id=c["event_id"])
        except Exception as exc:   # per-candidate fail-open: leave unresolved → retry
            log(f"atomic_graduate failed for {c.get('title')!r}: {exc!r} — retry next run")
    return {"mode": "ran", "created": created, "merged": merged, "skipped": skipped,
            "quarantined": quarantined, "clustered": 0}


# --------------------------------------------------------------------------- #
# Production bindings (the beat wires these into run_atomic_graduate_pass).
# --------------------------------------------------------------------------- #

def _cap_from_env(env) -> int:
    try:
        return int((env.get("ATOMIC_GRADUATE_CAP") or "").strip() or DEFAULT_CAP)
    except ValueError:
        return DEFAULT_CAP


def _float_env(env, key, default) -> float:
    try:
        return float((env.get(key) or "").strip() or default)
    except ValueError:
        return default


def _index_new_page(conn, *, slug, topic, title, signal, body, embedder):
    """Make a just-created atomic recall-reachable THIS run: upsert its unified_index row
    (BM25) + embed its ## Signal (knowledge_signal). Minimal one-page mirror of wiki_sync;
    fail-soft (the daily mirror-sync re-indexes it canonically anyway)."""
    try:
        from ultra_memory import retrieval_core
        bm = f"{title}\n{body}\n{signal}"
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO unified_index (slug, topic, page_type, title, snippet, bm25_text, "
            "frontmatter, path, content_sha256, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(slug) DO UPDATE SET title=excluded.title, snippet=excluded.snippet, "
            "bm25_text=excluded.bm25_text, updated_at=excluded.updated_at",
            (slug, topic, "mechanism", title, body[:400], bm, "{}",
             f"/wiki/{topic}/concepts/{slug}.md", "ag-" + slug, ""))
        conn.execute("COMMIT")
        if embedder is not None:
            retrieval_core.get_or_embed_batch(
                conn, [("knowledge_signal", slug, signal)], embedder=embedder)
            conn.commit()
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass


def _quarantine_page(path):
    """Flag a not-recall-findable auto-atomic as quarantined (archive-never-delete) — a
    `status: quarantined` frontmatter field (a small, allowed direct edit). Fail-soft."""
    try:
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        if text.startswith("---\n") and "\nstatus:" not in text.split("---\n", 2)[1]:
            p.write_text(text.replace("---\n", "---\nstatus: quarantined\n", 1),
                         encoding="utf-8")
    except Exception:
        pass


def beat(conn, config, ts, env):
    """The Atomic-Graduation Tier-2 beat. Default ON (kill-switch ATOMIC_GRADUATE_DISABLE);
    a no-op when there is nothing to graduate (returns before resolving config/gateway, so
    it never loads fastembed for an empty queue). Fail-open. Returns a summary dict."""
    if not _enabled(env):
        return {"mode": "disabled", "created": 0, "merged": 0, "skipped": 0, "quarantined": 0}
    if not pending_atomic_candidates(conn, limit=1):
        return {"mode": "ran", "created": 0, "merged": 0, "skipped": 0, "quarantined": 0}

    import subprocess
    import sys
    import tempfile

    from ultra_memory import recall as recall_mod
    from ultra_memory import retrieval_core, unified_query
    from ultra_memory.maintenance.wiki_curate import _active_roots, _resolve_gateway

    roots = _active_roots(config)
    if not roots or getattr(config, "wiki_gateway", None) is None:
        return {"mode": "ran", "created": 0, "merged": 0, "skipped": 0, "quarantined": 0,
                "skipped_reason": "no-wiki-gateway"}
    wiki_root = roots[0]
    gateway_prefix = _resolve_gateway(config.wiki_gateway, config)
    cwd = str(config.project_dir)

    def gateway_run(verb, args, content):
        cmd = [*gateway_prefix, verb, *args]
        tmp = None
        if content is not None:
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                             encoding="utf-8") as tf:
                tf.write(content)
                tmp = tf.name
            cmd += ["--from-file", tmp]
        try:
            subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
        finally:
            if tmp:
                Path(tmp).unlink(missing_ok=True)

    try:
        embedder = retrieval_core.default_embedder()
    except Exception:
        embedder = None

    def recall_fn(signal, top_k=DEFAULT_EVAL_TOP_N):
        return recall_mod.recall(signal, top_k=top_k, conn=conn, embedder=embedder,
                                 knowledge_only=True, build_embedder=False)

    # Topic + theme are CONSUMER-domain concepts — the engine stays domain-agnostic and
    # reads them from config (no consumer literals here). topics from config.topics;
    # kind->theme from config.atomic_graduate_themes (falls back to the candidate kind).
    topics = list(getattr(config, "topics", None) or [])
    theme_map = getattr(config, "atomic_graduate_themes", None) or {}
    return run_atomic_graduate_pass(
        conn, ts=ts, env=env, gateway_run=gateway_run,
        signal_match=unified_query.best_signal_match, wiki_root=wiki_root,
        recall_fn=recall_fn, index_fn=_index_new_page, quarantine_fn=_quarantine_page,
        embedder=embedder, cap=_cap_from_env(env),
        dedup_upper=_float_env(env, "ATOMIC_GRADUATE_DEDUP_UPPER", DEFAULT_DEDUP_UPPER),
        dedup_lower=_float_env(env, "ATOMIC_GRADUATE_DEDUP_LOWER", DEFAULT_DEDUP_LOWER),
        cluster_cos=_float_env(env, "ATOMIC_GRADUATE_CLUSTER_COS", DEFAULT_CLUSTER_COS),
        valid_topics=set(topics) if topics else None,
        default_topic=topics[0] if topics else "default", theme_map=theme_map,
        log=lambda m: print(f"[atomic_graduate] {m}", file=sys.stderr))
