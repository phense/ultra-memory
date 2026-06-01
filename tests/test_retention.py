import sqlite3

import pytest

from ultra_memory import memory_lib, retention


def test_prune_rolls_old_events_into_summary_then_deletes(tmp_path):
    conn = memory_lib.open_memory_db(str(tmp_path / "m.db"))
    memory_lib.record_session_event(conn, session_id="s1", kind="task_done",
                                    title="Old thing", ts="2026-01-01T00:00:00Z")
    memory_lib.record_session_event(conn, session_id="s1", kind="task_done",
                                    title="Recent thing", ts="2026-05-30T00:00:00Z")
    deleted = retention.prune_session_events(conn, keep_days=30,
                                             ts="2026-05-30T12:00:00Z")
    assert deleted == 1
    remaining = conn.execute("SELECT title FROM session_events").fetchall()
    assert [r[0] for r in remaining] == ["Recent thing"]
    summary = conn.execute("SELECT summary FROM sessions WHERE id='s1'").fetchone()[0]
    assert "Old thing" in summary
    conn.close()


def test_prune_noop_when_all_recent(tmp_path):
    conn = memory_lib.open_memory_db(str(tmp_path / "m.db"))
    memory_lib.record_session_event(conn, session_id="s1", kind="task_done",
                                    title="Recent", ts="2026-05-30T00:00:00Z")
    assert retention.prune_session_events(conn, keep_days=30,
                                          ts="2026-05-30T12:00:00Z") == 0
    conn.close()


# ---------------------------------------------------------------------------
# SP-8 bughunt FIX 1 — retention must NOT hard-delete an old session_event that is
# still referenced by an attribution edge (`src_kind='session_event'`,
# predicate IN validated_as/superseded_by/informed_by). Pruning it leaves a
# DANGLING link and the downstream EWMA fold (JOIN session_events se ON
# se.id = CAST(l.src_id AS INTEGER) ... WHERE se.outcome_signal IS NOT NULL)
# silently loses the evidence (outcome_signal is dropped from the rolled summary).
# ---------------------------------------------------------------------------

# The contract JOIN the downstream EWMA fold runs (attribution.py module docstring).
_CONTRACT_JOIN = (
    "SELECT se.ts, se.outcome_signal "
    "FROM links l "
    "JOIN session_events se ON se.id = CAST(l.src_id AS INTEGER) "
    "WHERE l.dst_kind='memory' AND l.dst_id=? AND l.src_kind='session_event' "
    "AND l.predicate IN ('validated_as','superseded_by','informed_by') "
    "AND se.outcome_signal IS NOT NULL"
)


def test_prune_preserves_referenced_outcome_event_but_prunes_unreferenced(tmp_path):
    """An OLD outcome session_event that is the src of an `informed_by` edge must
    SURVIVE the prune (event + outcome_signal intact, the contract JOIN still
    resolves), while an OLD UNREFERENCED event is still pruned as before."""
    conn = memory_lib.open_memory_db(str(tmp_path / "m.db"))
    # An old outcome event carrying a deterministic signal, referenced by an edge.
    memory_lib.record_session_event(
        conn, session_id="s1", kind="session_outcome", title="loss outcome",
        ts="2026-01-01T00:00:00Z", outcome_signal="trade_loss")
    referenced_id = conn.execute(
        "SELECT id FROM session_events WHERE title='loss outcome'").fetchone()[0]
    memory_lib.record_link(
        conn, src_kind="session_event", src_id=str(referenced_id),
        predicate="informed_by", dst_kind="memory", dst_id="m1",
        ts="2026-01-01T01:00:00Z")
    # An old UNREFERENCED event (must still be pruned).
    memory_lib.record_session_event(
        conn, session_id="s1", kind="task_done", title="orphan old",
        ts="2026-01-02T00:00:00Z")

    deleted = retention.prune_session_events(conn, keep_days=90,
                                             ts="2026-05-30T12:00:00Z")
    assert deleted == 1  # only the unreferenced old event

    # The referenced event survives intact.
    survivor = conn.execute(
        "SELECT outcome_signal FROM session_events WHERE id=?",
        (referenced_id,)).fetchone()
    assert survivor is not None, "referenced outcome event must survive the prune"
    assert survivor[0] == "trade_loss"

    # It was NOT rolled-and-dropped into the summary either.
    summary_row = conn.execute(
        "SELECT summary FROM sessions WHERE id='s1'").fetchone()
    summary = summary_row[0] if summary_row else ""
    assert "loss outcome" not in (summary or ""), \
        "preserved event must not be summarized-away"
    assert "orphan old" in (summary or "")  # the pruned one IS rolled

    # The contract JOIN the downstream EWMA fold runs still returns the evidence.
    joined = conn.execute(_CONTRACT_JOIN, ("m1",)).fetchall()
    assert [(r[0], r[1]) for r in joined] == [("2026-01-01T00:00:00Z", "trade_loss")]
    conn.close()


# ---------------------------------------------------------------------------
# FIX 2 — prune_session_events must route through the SAME bounded busy-retry as
# memory_lib._write_txn. A raw BEGIN IMMEDIATE that hits a transient SQLITE_BUSY
# (a writer holding the lock past the busy_timeout window) used to raise
# "database is locked" immediately; it must now retry-with-backoff and succeed.
# ---------------------------------------------------------------------------


def _lock_holder(db_path):
    """A 2nd connection holding the WAL write lock (BEGIN IMMEDIATE + a real write)
    so any other writer's BEGIN IMMEDIATE gets SQLITE_BUSY until released."""
    holder = sqlite3.connect(str(db_path))
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(
        "INSERT INTO meta (key, value) VALUES ('lockprobe','1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    return holder


def test_prune_retries_one_busy_then_succeeds(tmp_path, monkeypatch):
    """FIX 2: a single transient SQLITE_BUSY on prune's BEGIN IMMEDIATE is retried
    (after the lock releases during a backoff sleep) and the prune SUCCEEDS, instead
    of raising 'database is locked'."""
    db_path = tmp_path / "m.db"
    conn = memory_lib.open_memory_db(str(db_path))
    memory_lib.record_session_event(conn, session_id="s1", kind="task_done",
                                    title="Old thing", ts="2026-01-01T00:00:00Z")
    # Surface BUSY at once instead of waiting out the 30s busy_timeout.
    conn.execute("PRAGMA busy_timeout=0")

    holder = _lock_holder(db_path)
    # Release the held lock on the FIRST backoff sleep so attempt #2 wins.
    state = {"released": False}

    def release_then_sleep(_secs):
        if not state["released"]:
            holder.rollback()
            holder.close()
            state["released"] = True

    monkeypatch.setattr(memory_lib.time, "sleep", release_then_sleep)

    deleted = retention.prune_session_events(conn, keep_days=30,
                                             ts="2026-05-30T12:00:00Z")
    assert state["released"], "the retry path must have been exercised (a sleep fired)"
    assert deleted == 1
    assert conn.execute("SELECT COUNT(*) FROM session_events").fetchone()[0] == 0
    conn.close()
