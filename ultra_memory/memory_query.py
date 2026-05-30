"""Read-side memory retrieval (spec §8, D11) — LEAN: embedding-cosine + title-index.

BM25/RRF/reranker for memory are deferred behind the eval harness (D11). No LLM.
Every entry point takes an injected `embedder` (list[str] -> list[list[float]]).
"""
from . import retrieval_core as rc

_DEFAULT_STATUSES = ("active",)
_TITLE_BOOST = 0.5


def _doc_text(row):
    return f"{row['title']}\n{row['body']}"


def _title_hit(title, query):
    if not title:
        return False
    t = title.lower()
    q = query.lower()
    return t in q or q in t


def query_memories(conn, query, *, embedder, top_k=5, dim=rc.EMBED_DIM,
                   include_statuses=_DEFAULT_STATUSES, now_ts=None,
                   staleness_days=90):
    """Rank active memories for `query`. Returns a list of JSON-serialisable dicts
    ordered by final score desc (cosine + title boost, ranking signals in Task 5)."""
    placeholders = ",".join("?" * len(include_statuses))
    rows = conn.execute(
        f"SELECT * FROM memories WHERE status IN ({placeholders})",
        tuple(include_statuses),
    ).fetchall()
    if not rows:
        return []

    items = []
    by_id = {}
    for r in rows:
        vec = rc.get_or_embed(conn, target_kind="memory", target_id=r["id"],
                              text=_doc_text(r), embedder=embedder, dim=dim)
        items.append((r["id"], vec))
        by_id[r["id"]] = r

    q_vec = embedder([query])[0]
    relevance = dict(rc.cosine_search(q_vec, items))

    results = []
    for mid, r in by_id.items():
        score = relevance.get(mid, 0.0)
        if _title_hit(r["title"], query):
            score += _TITLE_BOOST
        results.append({
            "id": mid,
            "title": r["title"],
            "type": r["type"],
            "status": r["status"],
            "score": score,
        })
    results.sort(key=lambda d: d["score"], reverse=True)
    return results[:top_k]
