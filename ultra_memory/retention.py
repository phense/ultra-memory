"""session_events retention (spec §8 D11): roll old events into the session
summary, then delete them. The summary keeps a durable digest; raw rows are
bounded so session_events (where the real growth is) cannot grow unboundedly."""
import datetime


def _cutoff(ts, keep_days):
    base = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)
    return (base - datetime.timedelta(days=keep_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def prune_session_events(conn, *, keep_days, ts):
    """Delete events older than keep_days after rolling them into sessions.summary.
    Returns the number of events deleted."""
    cutoff = _cutoff(ts, keep_days)
    old = conn.execute(
        "SELECT id, session_id, ts, kind, title FROM session_events "
        "WHERE ts < ? ORDER BY session_id, ts", (cutoff,)
    ).fetchall()
    if not old:
        return 0
    by_session = {}
    for _id, sid, ets, kind, title in old:
        by_session.setdefault(sid, []).append(f"[{ets[:10]} {kind}] {title}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for sid, digests in by_session.items():
            row = conn.execute("SELECT summary FROM sessions WHERE id=?", (sid,)).fetchone()
            prior = (row[0] + "\n") if row and row[0] else ""
            rolled = prior + "Archived events:\n" + "\n".join(digests)
            conn.execute(
                "INSERT INTO sessions (id, summary) VALUES (?,?) "
                "ON CONFLICT(id) DO UPDATE SET summary=excluded.summary",
                (sid, rolled))
        conn.execute("DELETE FROM session_events WHERE ts < ?", (cutoff,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(old)
