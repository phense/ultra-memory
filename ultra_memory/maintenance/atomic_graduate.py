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
_GOTCHA_THEME = "tooling"
_LESSON_THEME = "strategy-methodology"
_SOURCE_LABEL = "atomic-graduate"


def _enabled(env) -> bool:
    """Default ON; disabled only when `ATOMIC_GRADUATE_DISABLE` is set (the kill-switch)."""
    return not (env.get("ATOMIC_GRADUATE_DISABLE") or "").strip()


def _slugify(title: str, signal: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:60] or "atomic"
    h = hashlib.sha1((title + "\x00" + signal).encode("utf-8")).hexdigest()[:4]
    return f"{base}-{h}"


def _theme_for(cand) -> str:
    return _GOTCHA_THEME if cand.get("kind") == "gotcha" else _LESSON_THEME


def _render_atomic(cand, *, slug, ts) -> str:
    """The atomic markdown: frontmatter + body + a `## Signal` section. A `lesson` (e.g.
    trading/strategy) carries an unvalidated confidence label + the entry-trigger
    disambiguation so a real-money path never treats an auto-created lesson as established."""
    theme = _theme_for(cand)
    kind = cand.get("kind")
    tags = "[engineering, tooling, auto-graduated]" if kind == "gotcha" \
        else "[strategy, auto-graduated]"
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


def run_atomic_graduate_pass(conn, *, ts, env, gateway_run, signal_match, wiki_root,
                             recall_fn=None, index_fn=None, quarantine_fn=None,
                             embedder=None, cap=DEFAULT_CAP,
                             dedup_upper=DEFAULT_DEDUP_UPPER, dedup_lower=DEFAULT_DEDUP_LOWER,
                             eval_top_n=DEFAULT_EVAL_TOP_N, valid_topics=None,
                             default_topic="trading", log=lambda _m: None) -> dict:
    """Drain `atomic_candidate` markers → create / merge / skip `## Signal`-keyed atomics,
    capped + fenced + fail-open. `gateway_run(verb, args:list[str], content:str|None)` is
    injected (production: shells the consumer gateway; test: a recorder)."""
    if not _enabled(env):
        return {"mode": "disabled", "created": 0, "merged": 0, "skipped": 0, "quarantined": 0}
    created = merged = skipped = quarantined = 0
    pend = pending_atomic_candidates(conn, limit=max(cap * 10, 50))
    for c in pend:
        if created >= cap:
            log(f"atomic_graduate: blast-radius cap {cap} reached — "
                f"{len(pend) - cap} candidate(s) left for next run")
            break
        # Normalize the candidate topic against the known topics: the extraction may
        # put a THEME (e.g. "tooling") in the topic field, which would otherwise create
        # a spurious top-level topic tree. Unknown → the consumer's default topic.
        raw_topic = c.get("topic") or default_topic
        topic = raw_topic if (valid_topics is None or raw_topic in valid_topics) \
            else default_topic
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
                        _render_atomic(c, slug=slug, ts=ts))
            gateway_run("register-index",
                        ["--slug", slug, "--theme", _theme_for(c),
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
            "quarantined": quarantined}


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

    topics = list(getattr(config, "topics", None) or ["trading"])
    return run_atomic_graduate_pass(
        conn, ts=ts, env=env, gateway_run=gateway_run,
        signal_match=unified_query.best_signal_match, wiki_root=wiki_root,
        recall_fn=recall_fn, index_fn=_index_new_page, quarantine_fn=_quarantine_page,
        embedder=embedder, cap=_cap_from_env(env),
        dedup_upper=_float_env(env, "ATOMIC_GRADUATE_DEDUP_UPPER", DEFAULT_DEDUP_UPPER),
        dedup_lower=_float_env(env, "ATOMIC_GRADUATE_DEDUP_LOWER", DEFAULT_DEDUP_LOWER),
        valid_topics=set(topics), default_topic=topics[0],
        log=lambda m: print(f"[atomic_graduate] {m}", file=sys.stderr))
