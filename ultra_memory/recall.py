"""Recall-Reflex: the public ``recall()`` primitive.

> Recognise a situation -> recall what you know about it -> act informed.

``recall(signal_text)`` returns a frugal, uniform list of atomic/memory snippets
for an observed signal, via the engine's :func:`unified_query.unified_recall`
(BM25 + embedding-cosine + FU-4 RRF over the wiki mirror + memory store, scoped by
the type x topic privilege wall, redacted + audited). It is the single shared
entry point for every consumer: the engineering ``UserPromptSubmit`` hook and the
trading observation surfaces both call it.

Posture:
  * **Fail-open** — any error returns ``[]`` + one stderr diagnostic; never raises.
  * **Privacy** — ``caller_class`` defaults to ``"subagent"`` (SAFE_TYPES =
    project/reference); a main-session caller that wants user/feedback must opt in
    explicitly. The type wall is enforced inside ``unified_recall``.
"""
import json
import os
import sys

from . import knowledge_mcp, retrieval_core, unified_query

# Navigational page-types are noise as recalled "prior art" — never a lesson.
_EXCLUDED_PAGE_TYPES = ("index", "redirect")


def _to_hit(row):
    """Map a ``unified_recall`` row to a uniform, frugal hit dict.

    Knowledge rows carry ``source_kind='knowledge'`` + slug/snippet/path; memory
    rows carry ``source_kind='memory'`` + id; the memory-only byte-identity path
    has NO ``source_kind`` (every such row is a memory id)."""
    if row.get("source_kind") == "knowledge":
        return {
            "source_kind": "knowledge",
            "slug": row.get("slug"),
            "title": row.get("title") or "",
            "snippet": row.get("snippet") or "",
            "path": row.get("path"),
            "page_type": row.get("page_type"),
            "topic": row.get("topic"),
            "score": row.get("score"),
        }
    return {
        "source_kind": "memory",
        "id": row.get("id"),
        "title": row.get("title") or "",
        "snippet": "",
        "type": row.get("type"),
        "stale": row.get("stale"),
        "score": row.get("score"),
    }


def recall(signal_text, *, top_k=5, caller_class="subagent", agent_topics=None,
           db_path=None, embedder=None, build_embedder=True, knowledge_only=False,
           exclude_page_types=_EXCLUDED_PAGE_TYPES, conn=None, now_ts=None):
    """Return up to ``top_k`` frugal hits for an observed signal. Fail-open -> [].

    Args:
      signal_text: the observed condition (error text / market condition).
      top_k: max hits.
      caller_class: privilege class for the type wall (default 'subagent' =
        SAFE_TYPES; pass 'orchestrator'/'owner' only on a trusted human path).
      agent_topics: a set of topics to scope to, or None for all topics.
      db_path: memory.db path (default: knowledge_mcp.db_path_from_env).
      embedder: list[str]->list[list[float]]; if None and build_embedder, a lazy
        fastembed embedder is built (fail-soft to None = BM25-only knowledge).
      conn: an open sqlite3 connection (test/embedded use); else one is opened.
    """
    try:
        own_conn = False
        if conn is None:
            from . import db
            path = db_path or knowledge_mcp.db_path_from_env(os.environ)
            conn = db.connect(path)
            own_conn = True
        try:
            if embedder is None and build_embedder:
                try:
                    embedder = retrieval_core.default_embedder()
                except Exception:
                    embedder = None  # fail-soft: BM25-only knowledge recall
            # Over-fetch so the page-type filter can drop navigational pages without
            # starving the result below top_k.
            excluded = set(exclude_page_types or ())
            fetch_k = min(max(top_k * 3, top_k), 50) if excluded else top_k
            hits = unified_query.unified_recall(
                conn, signal_text, caller_class=caller_class,
                agent_topics=agent_topics, embedder=embedder, top_k=fetch_k,
                now_ts=now_ts, ts=now_ts, include_memory=not knowledge_only)
            out = [_to_hit(h) for h in hits
                   if h.get("page_type") not in excluded]
            return out[:top_k]
        finally:
            if own_conn:
                try:
                    conn.close()
                except Exception:
                    pass
    except Exception as exc:  # fail-open: a recall error never propagates
        print(f"recall: failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return []


def main(argv=None):
    """CLI: ``python -m ultra_memory.recall <signal> [--top N] [--topic t,u]
    [--caller-class C] [--no-embed] [--json]``. ALWAYS rc 0 (fail-open)."""
    import argparse

    ap = argparse.ArgumentParser(
        prog="ultra_memory.recall",
        description="Recall atomic/memory snippets for an observed signal.")
    ap.add_argument("signal", help="the observed condition (error text / market condition)")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--topic", default=None,
                    help="comma-separated topic scope (default: all topics)")
    ap.add_argument("--caller-class", default="subagent")
    ap.add_argument("--no-embed", action="store_true",
                    help="skip the embedder (BM25-only knowledge; no fastembed load)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    agent_topics = None
    if args.topic:
        agent_topics = {t.strip() for t in args.topic.split(",") if t.strip()}

    hits = recall(args.signal, top_k=args.top, caller_class=args.caller_class,
                  agent_topics=agent_topics, build_embedder=not args.no_embed)

    if args.json:
        print(json.dumps(hits))
    else:
        if not hits:
            print("(no recall hits)")
        for i, h in enumerate(hits, 1):
            label = h.get("slug") or h.get("id")
            print(f"{i}. [{h.get('source_kind')}] {label} — {h.get('title', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
