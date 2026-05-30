"""Read-side memory retrieval (spec §8, D11) — LEAN: embedding-cosine + title-index.

BM25/RRF/reranker for memory are deferred behind the eval harness (D11). No LLM.
Every entry point takes an injected `embedder` (list[str] -> list[list[float]]).
"""
from datetime import datetime

from . import retrieval_core as rc

_DEFAULT_STATUSES = ("active",)
_TITLE_BOOST = 0.5
_ACCESS_BOOST = 0.02      # per access, bounded
_ACCESS_CAP = 10
_STALE_PENALTY = 0.2


def _doc_text(row):
    return f"{row['title']}\n{row['body']}"


def _days_between(later_ts, earlier_ts):
    if not later_ts or not earlier_ts:
        return 0
    try:
        a = datetime.fromisoformat(later_ts)
        b = datetime.fromisoformat(earlier_ts)
    except ValueError:
        return 0
    return (a - b).days


def _links_for(conn, mid):
    rows = conn.execute(
        "SELECT predicate, dst_kind, dst_id FROM links "
        "WHERE src_kind='memory' AND src_id=? ORDER BY rowid", (mid,)).fetchall()
    return [{"predicate": r["predicate"], "dst_kind": r["dst_kind"],
             "dst_id": r["dst_id"]} for r in rows]


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
        score *= (r["strength"] if r["strength"] is not None else 1.0)
        score += _ACCESS_BOOST * min(r["access_count"] or 0, _ACCESS_CAP)
        age = _days_between(now_ts, r["updated_at"])
        stale = age > staleness_days
        if stale:
            score -= _STALE_PENALTY
        results.append({
            "id": mid,
            "title": r["title"],
            "type": r["type"],
            "status": r["status"],
            "score": score,
            "stale": stale,
            "links": _links_for(conn, mid),
        })
    results.sort(key=lambda d: d["score"], reverse=True)
    return results[:top_k]
