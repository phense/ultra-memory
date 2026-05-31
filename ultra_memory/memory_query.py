"""Read-side memory retrieval (spec §8, D11) — LEAN: embedding-cosine + title-index.

BM25/RRF/reranker for memory are deferred behind the eval harness (D11). No LLM.
Every entry point takes an injected `embedder` (list[str] -> list[list[float]]).
"""
import re
from datetime import datetime, timezone

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
        # Normalize to naive UTC so an aware/naive mix computes the REAL age instead
        # of raising (and being swallowed to 0). Production callers pass a tz-aware
        # `now` while stored timestamps are naive-UTC; swallowing that mismatch
        # silently killed the staleness signal everywhere.
        if a.tzinfo is not None:
            a = a.astimezone(timezone.utc).replace(tzinfo=None)
        if b.tzinfo is not None:
            b = b.astimezone(timezone.utc).replace(tzinfo=None)
        return (a - b).days
    except (ValueError, TypeError):
        # Genuinely unparseable timestamp — treat as 0 days rather than crashing.
        return 0


def _links_for(conn, mid):
    rows = conn.execute(
        "SELECT predicate, dst_kind, dst_id FROM links "
        "WHERE src_kind='memory' AND src_id=? ORDER BY rowid", (mid,)).fetchall()
    return [{"predicate": r["predicate"], "dst_kind": r["dst_kind"],
             "dst_id": r["dst_id"]} for r in rows]


def _title_hit(title, query):
    """True iff the title appears as a whole token in the query (or vice-versa).
    Word-bounded so short titles like 'car'/'new'/'test' don't spuriously match
    inside 'oscar'/'newsletter'/'backtest'."""
    if not title:
        return False
    t = title.lower().strip()
    q = query.lower().strip()
    if not t or not q:
        return False

    def _whole(needle, hay):
        return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", hay) is not None

    return _whole(t, q) or _whole(q, t)


def query_memories(conn, query, *, embedder, top_k=5, dim=rc.EMBED_DIM,
                   include_statuses=_DEFAULT_STATUSES, include_types=None,
                   now_ts=None, staleness_days=90, topic=None):
    """Rank active memories for `query`. Returns a list of JSON-serialisable dicts
    ordered by final score desc (cosine + title boost + ranking signals).

    `include_types`, when given, scopes the candidate set in SQL BEFORE ranking and
    truncation — so a type-restricted caller (the knowledge MCP privilege boundary)
    is never starved by higher-ranked out-of-scope rows filling a fixed window.

    `topic` (SP-3 Stage 2, D11), when given, scopes candidates to that topic OR
    `topic IS NULL`: a topiced caller still sees the cross-topic (NULL) operational
    rows, and — critically — a corpus of un-topiced rows stays FULLY visible (NO
    retrieval regression). Omitting `topic` returns every row exactly as before. The
    topic axis is orthogonal to and composes (AND) with `include_types`.
    """
    top_k = max(0, int(top_k))
    if now_ts is None:
        # Default to now so the staleness signal is live for in-process callers that
        # omit now_ts (else _days_between short-circuits and `stale` is always False).
        now_ts = datetime.now(timezone.utc).isoformat()
    clauses = [f"status IN ({','.join('?' * len(include_statuses))})"]
    params = list(include_statuses)
    if include_types is not None:
        include_types = tuple(include_types)
        if not include_types:
            return []
        clauses.append(f"type IN ({','.join('?' * len(include_types))})")
        params += list(include_types)
    if topic is not None:
        # `topic IS NULL` is ALWAYS retained — cross-topic rows are visible to every
        # caller (D11), and an un-topiced corpus does not regress.
        clauses.append("(topic = ? OR topic IS NULL)")
        params.append(topic)
    rows = conn.execute(
        f"SELECT * FROM memories WHERE {' AND '.join(clauses)}",
        tuple(params),
    ).fetchall()
    if not rows:
        return []

    by_id = {r["id"]: r for r in rows}
    # Embed all cache-misses in one batched call + one write txn (audit L7).
    vecs = rc.get_or_embed_batch(
        conn, [("memory", r["id"], _doc_text(r)) for r in rows],
        embedder=embedder, dim=dim)

    q_vec = embedder([query])[0]
    if len(q_vec) != dim:
        raise ValueError(
            f"query embedding dim {len(q_vec)} != expected {dim}; embedder/model mismatch")
    relevance = dict(rc.cosine_search(q_vec, list(vecs.items())))

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
