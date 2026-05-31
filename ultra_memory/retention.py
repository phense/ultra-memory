"""session_events retention (spec §8 D11): roll old events into the session
summary, then delete them. The summary keeps a durable digest; raw rows are
bounded so session_events (where the real growth is) cannot grow unboundedly."""
import datetime

# Bound the rolled digest so sessions.summary can't grow unboundedly across runs.
_SUMMARY_MAX_LINES = 200


def _cutoff(ts, keep_days):
    base = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)
    return (base - datetime.timedelta(days=keep_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def prune_session_events(conn, *, keep_days, ts):
    """Delete events older than keep_days after rolling them into sessions.summary.
    Returns the number of events deleted."""
    cutoff = _cutoff(ts, keep_days)
    # Snapshot + roll-up + delete all run inside ONE BEGIN IMMEDIATE so a row
    # inserted between selecting and deleting can't be deleted-without-archiving.
    conn.execute("BEGIN IMMEDIATE")
    try:
        old = conn.execute(
            "SELECT id, session_id, ts, kind, title FROM session_events "
            "WHERE ts < ? ORDER BY session_id, ts", (cutoff,)
        ).fetchall()
        if not old:
            conn.execute("COMMIT")
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
        cur = conn.execute("DELETE FROM session_events WHERE ts < ?", (cutoff,))
        deleted = cur.rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return deleted
