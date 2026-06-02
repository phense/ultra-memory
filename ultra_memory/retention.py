"""session_events retention (spec §8 D11): roll old events into the session
summary, then delete them. The summary keeps a durable digest; raw rows are
bounded so session_events (where the real growth is) cannot grow unboundedly."""
import datetime

from . import memory_lib
from ._time import ZULU_FMT

# Bound the rolled digest so sessions.summary can't grow unboundedly across runs.
_SUMMARY_MAX_LINES = 200

# SP-8 attribution edges anchor on a session_event as `src` (src_kind='session_event',
# src_id=str(session_events.id)). An event referenced by one of these predicates is
# EVIDENCE the downstream EWMA fold reads (JOIN session_events se ON
# se.id = CAST(l.src_id AS INTEGER) ... WHERE se.outcome_signal IS NOT NULL).
# Pruning it (delete OR roll-and-drop, which loses outcome_signal) silently destroys
# that evidence and leaves a dangling link — so such events are EXCLUDED from prune.
# Module constant so the predicate set stays maintainable alongside attribution.py.
_ATTRIBUTION_PREDICATES = ("validated_as", "superseded_by", "informed_by")
# Constant placeholder list for the predicate IN-clause — built once at import.
_ATTRIBUTION_PRED_PH = ",".join("?" for _ in _ATTRIBUTION_PREDICATES)


def _cutoff(ts, keep_days):
    base = datetime.datetime.strptime(ts, ZULU_FMT).replace(
        tzinfo=datetime.timezone.utc)
    return (base - datetime.timedelta(days=keep_days)).strftime(ZULU_FMT)


def prune_session_events(conn, *, keep_days, ts):
    """Delete events older than keep_days after rolling them into sessions.summary.
    Returns the number of events deleted."""
    cutoff = _cutoff(ts, keep_days)
    # Exclude any event still referenced by an SP-8 attribution edge (it is EVIDENCE
    # the downstream EWMA fold reads — deleting or rolling-and-dropping it would lose
    # outcome_signal and dangle the link). Applied to BOTH the roll SELECT and the
    # DELETE so a preserved event is neither summarized-away nor deleted.
    not_referenced = (
        "NOT EXISTS (SELECT 1 FROM links l "
        "WHERE l.src_kind='session_event' "
        "AND CAST(l.src_id AS INTEGER) = session_events.id "
        f"AND l.predicate IN ({_ATTRIBUTION_PRED_PH}))"
    )
    # Snapshot + roll-up + delete all run inside ONE BEGIN IMMEDIATE so a row
    # inserted between selecting and deleting can't be deleted-without-archiving.
    # Route through the engine's shared bounded busy-retry discipline (the same loop
    # memory_lib._write_txn uses) so a transient SQLITE_BUSY from a writer holding the
    # lock past the busy_timeout window is retried-with-backoff, not raised at once.
    # work() is re-runnable: every attempt re-selects from scratch on a fresh txn
    # (the prior attempt was rolled back), so the snapshot can't go stale. No spool —
    # this is an idempotent maintenance write; a final exhaustion still raises (caught
    # by maintain.run's broad try/except, preserving the existing fail-open behavior).
    def work():
        old = conn.execute(
            "SELECT id, session_id, ts, kind, title FROM session_events "
            f"WHERE ts < ? AND {not_referenced} ORDER BY session_id, ts",
            (cutoff, *_ATTRIBUTION_PREDICATES)
        ).fetchall()
        if not old:
            return 0
        by_session = {}
        for _id, sid, ets, kind, title in old:
            by_session.setdefault(sid, []).append(f"[{ets[:10]} {kind}] {title}")
        for sid, digests in by_session.items():
            row = conn.execute("SELECT summary FROM sessions WHERE id=?", (sid,)).fetchone()
            prior = (row[0] + "\n") if row and row[0] else ""
            rolled = prior + "Archived events:\n" + "\n".join(digests)
            # Keep only the most recent lines so the digest stays bounded.
            rolled = "\n".join(rolled.splitlines()[-_SUMMARY_MAX_LINES:])
            conn.execute(
                "INSERT INTO sessions (id, summary) VALUES (?,?) "
                "ON CONFLICT(id) DO UPDATE SET summary=excluded.summary",
                (sid, rolled))
        cur = conn.execute(
            f"DELETE FROM session_events WHERE ts < ? AND {not_referenced}",
            (cutoff, *_ATTRIBUTION_PREDICATES))
        return cur.rowcount

    return memory_lib._with_immediate_retry(conn, work)
