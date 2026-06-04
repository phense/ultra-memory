"""SP-8 stage A2 — the usage-outcome ATTRIBUTION JOIN (deterministic, NO LLM).

When a Claude session ends, an outcome `session_event` is written carrying an
`outcome_signal` (e.g. 'tests_passed', 'trade_win'). Separately, during the session,
every recalled memory was logged to `access_log` with that session's `session_id`
and a 1-based fused `rank` (stage A1). This module is the engine primitive that, at
session-end, JOINs the session's recalled memories to that outcome event by writing
`informed_by` graph edges — one per policy-selected recalled memory. A downstream
consumer (consumer-side, NOT this engine) then folds those edges into an EWMA.

PROJECT-AGNOSTIC (hard NFR): this module imports only stdlib + `from . import
memory_lib`. There is NO policy config and NO Trading/wiki concept here — the
consumer supplies `policy`/`k` as parameters. The default (`policy='top_k', k=1`)
is the conservative recommendation: attribute only the single most-relevant recall.

THE INTEGRATION CONTRACT (the crux). The downstream consumer reads the edges with::

    SELECT se.ts, se.outcome_signal
      FROM links l
      JOIN session_events se ON se.id = CAST(l.src_id AS INTEGER)
     WHERE l.dst_kind='memory' AND l.dst_id=? AND l.src_kind='session_event'
       AND l.predicate IN (...,'informed_by') AND se.outcome_signal IS NOT NULL

So an `informed_by` edge MUST be `src_kind='session_event'`, `src_id =
str(<session_events.id>)`, `predicate='informed_by'`, `dst_kind='memory'`,
`dst_id=<memory id>`. `attribute_usage` writes exactly that shape; the integer
`session_events.id` is resolved upstream via `memory_lib.event_id_for_key`.

NO LLM anywhere on this path: it is a deterministic SQL read + a pure selection
function + idempotent edge writes. It is FAIL-OPEN — it runs in a session-end Stop
hook and must never wedge a session, so any error degrades to a no-op (0 edges).
"""
from . import memory_lib

# Engine stays config-free: the policies are PARAMETERS, not tuned constants.
_KNOWN_POLICIES = ("all", "top_k")


def recalled_units_for_session(conn, *, session_id):
    """Return the session's recalled MEMORY units as a list of ``{'id', 'rank'}``
    dicts — one row per `access_log` entry with `target_kind='memory'`, this
    `session_id`, and a non-NULL `rank` (a knowledge recall, another session's
    recall, and a NULL-rank access are all excluded). Ordered by `(rank, id)` for
    determinism. Read-only; FAIL-CLOSED-TO-EMPTY — a read error returns ``[]``,
    never raises."""
    try:
        rows = conn.execute(
            "SELECT target_id AS id, rank FROM access_log "
            "WHERE target_kind='memory' AND session_id=? AND rank IS NOT NULL "
            "ORDER BY rank, target_id",
            (session_id,),
        ).fetchall()
    except Exception:
        return []
    return [{"id": r["id"], "rank": r["rank"]} for r in rows]


def apply_attribution_policy(rows, *, policy="top_k", k=1):
    """PURE function (no DB). Given recalled rows ``[{'id', 'rank'}, ...]`` select
    the DISTINCT memory ids per policy:

      - ``'all'``    : every distinct recalled id, ordered by best (lowest) rank,
                       ties broken by id.
      - ``'top_k'``  : the ``k`` distinct ids with the LOWEST rank across the
                       session (dedup keeping each id's best rank; ties broken by
                       id for determinism). ``k >= 1``.

    An unknown policy raises ``ValueError`` (never silently attribute-all). Other
    policies (rank-weighted, scope='recall', 'applied') are deliberately NOT
    implemented — they need substrate that does not yet exist."""
    if policy not in _KNOWN_POLICIES:
        raise ValueError(
            f"unknown attribution policy {policy!r}; known: {_KNOWN_POLICIES}")

    # Dedup to each id's BEST (lowest) rank.
    best = {}
    for r in rows:
        rid, rank = r["id"], r["rank"]
        if rid not in best or rank < best[rid]:
            best[rid] = rank
    # Deterministic order: by best rank, ties broken by id.
    ordered = sorted(best, key=lambda rid: (best[rid], rid))

    if policy == "all":
        return ordered
    # top_k
    if k < 1:
        raise ValueError(f"top_k requires k>=1, got {k!r}")
    return ordered[:k]


def attribute_usage(conn, *, session_id, outcome_event_id, ts,
                    policy="top_k", k=1):
    """At session-end: write an `informed_by` edge from the outcome `session_event`
    to each policy-selected recalled memory. Returns the number of edges written.

    The edge is::

        record_link(src_kind='session_event', src_id=str(outcome_event_id),
                    predicate='informed_by', dst_kind='memory', dst_id=<id>, ts=ts)

    `outcome_event_id` is the INTEGER `session_events.id` (resolve via
    `memory_lib.event_id_for_key` upstream); if it is ``None`` this is a no-op
    (returns 0). Deterministic, NO LLM, idempotent (`record_link` upserts on the
    edge key — a re-run writes no duplicate). FAIL-OPEN: any error degrades to 0,
    never raises out (it must never wedge a Stop hook)."""
    if outcome_event_id is None:
        return 0
    try:
        rows = recalled_units_for_session(conn, session_id=session_id)
        selected = apply_attribution_policy(rows, policy=policy, k=k)
        src_id = str(outcome_event_id)
        written = 0
        for mid in selected:
            memory_lib.record_link(
                conn, src_kind="session_event", src_id=src_id,
                predicate="informed_by", dst_kind="memory", dst_id=mid, ts=ts)
            written += 1
        return written
    except Exception:
        # Fail-open: a session-end Stop hook must never wedge on attribution.
        return 0
