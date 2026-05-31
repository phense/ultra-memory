"""Integration tests for seam S6: retention vs. pins.

Exercises the REAL seam between the audited write path and the retention GC:

    memory_lib.open_memory_db        (real schema bootstrap / migrate)
    memory_lib.save_memory           (real audited memory write — the protected table)
    memory_lib.set_pinned            (real pin flag write)
    memory_lib.record_session_event  (real event write)
    retention.prune_session_events   (high-volume event-log GC + roll-up)

The seam's core invariant: retention is scoped to ``session_events`` only.
It must NEVER evict or mutate rows in the ``memories`` table — pinned,
hard-rule, ordinary, or even ordinary-rows-older-than-keep_days.  These tests
encode that scope and turn red the instant someone bolts memory eviction onto
the retention path.

Hermetic & deterministic: every test uses a tmp SQLite DB via pytest tmp_path,
passes an explicit timestamp (``ts=``) to prune so wall-clock never matters,
and seeds ``memories`` rows through the real audited write API.  No network, no
real data/memory.db, no ``claude`` CLI.

API ground-truth (read from ultra_memory/memory_lib.py + retention.py):
    conn = memory_lib.open_memory_db(str(path))                          # ml:87
    memory_lib.save_memory(conn, id=, type=, title=, body=, ts=, ...)    # ml:103
    memory_lib.set_pinned(conn, id=, pinned=, ts=)                       # ml:246
    memory_lib.record_session_event(conn, session_id=, kind=, title=, ts=)  # ml:158
    retention.prune_session_events(conn, keep_days=, ts=)  -> int        # ret:13
    # 'kind' is the memory TYPE column ('user' / 'hard-rule' / 'project' / ...);
    # session_events stores roll-up text into sessions.summary.
"""
from __future__ import annotations

import sqlite3

import pytest

from ultra_memory import memory_lib, retention


def _memory_snapshot(conn):
    """{id: (pinned, status, type)} for every memories row — the protected state."""
    rows = conn.execute("SELECT id, pinned, status, type FROM memories").fetchall()
    # sqlite3.Row -> tuple by index for stable equality comparison.
    return {r["id"]: (r["pinned"], r["status"], r["type"]) for r in rows}


def _save(conn, *, id, type, ts, pinned=False, created_at=None, updated_at=None):
    memory_lib.save_memory(
        conn, id=id, type=type, title=f"{id} title", body=f"{id} body",
        ts=ts, created_at=created_at, updated_at=updated_at,
    )
    if pinned:
        memory_lib.set_pinned(conn, id=id, pinned=True, ts=ts)


@pytest.fixture()
def conn(tmp_path):
    c = memory_lib.open_memory_db(str(tmp_path / "m.db"))
    yield c
    c.close()


# ---------------------------------------------------------------------------
# 1. The core seam invariant: prune never touches the memories table.
# ---------------------------------------------------------------------------
def test_prune_session_events_never_touches_memories_table(conn):
    """Pinned, hard-rule, ordinary, and OLD-ordinary memories all survive prune.

    Even the ordinary memory whose created_at/updated_at predate keep_days must
    survive: retention is scoped to session_events, full stop.  An old session
    event IS deleted in the same run, proving prune actually did its job.
    """
    now = "2026-05-30T12:00:00Z"
    # Four memories spanning the protection spectrum (written via the real API).
    _save(conn, id="m_pinned", type="user", ts=now, pinned=True)
    _save(conn, id="m_rule", type="hard-rule", ts=now, pinned=True)
    _save(conn, id="m_ord", type="project", ts=now)
    # OLD ordinary memory: created_at/updated_at far past keep_days=30.
    _save(conn, id="m_old", type="project", ts=now,
          created_at="2024-01-01T00:00:00Z", updated_at="2024-01-01T00:00:00Z")

    # Old + recent session events.
    memory_lib.record_session_event(conn, session_id="s1", kind="task_done",
                                    title="Old event", ts="2026-01-01T00:00:00Z")
    memory_lib.record_session_event(conn, session_id="s1", kind="task_done",
                                    title="Recent event", ts="2026-05-29T00:00:00Z")

    before = _memory_snapshot(conn)
    assert set(before) == {"m_pinned", "m_rule", "m_ord", "m_old"}
    # sanity: pins + the old row's age actually landed as intended.
    assert before["m_pinned"][0] == 1 and before["m_rule"][0] == 1
    assert before["m_rule"][2] == "hard-rule"

    deleted = retention.prune_session_events(conn, keep_days=30, ts=now)

    # The old session event was pruned; the recent one stayed.
    assert deleted == 1
    remaining = [r["title"] for r in conn.execute(
        "SELECT title FROM session_events").fetchall()]
    assert remaining == ["Recent event"]

    # ALL four memories survive untouched — pinned/status/type preserved.
    after = _memory_snapshot(conn)
    assert after == before, "retention must never mutate or evict memories rows"


# ---------------------------------------------------------------------------
# 2. Durability: a failure mid-prune rolls back the whole transaction.
# ---------------------------------------------------------------------------
def test_roll_up_then_delete_is_atomic_on_delete_failure(conn):
    """If the DELETE raises, the roll-up summary write must be rolled back too.

    prune_session_events rolls old events into sessions.summary then DELETEs them
    inside one BEGIN IMMEDIATE/COMMIT, with ROLLBACK on any exception
    (retention.py:26-40).  We wrap the connection so only the DELETE statement
    explodes (everything else delegates to the real conn).  The function must
    propagate the error AND leave the event present with no committed summary.
    """
    memory_lib.record_session_event(conn, session_id="s1", kind="task_done",
                                    title="To be rolled up", ts="2026-01-01T00:00:00Z")

    class FailingConn:
        """Delegates everything to the real conn, but fails on DELETE."""

        def __init__(self, inner):
            object.__setattr__(self, "_inner", inner)

        def execute(self, sql, *args):
            if sql.lstrip().upper().startswith("DELETE"):
                raise sqlite3.OperationalError("boom: simulated delete failure")
            return self._inner.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    failing = FailingConn(conn)

    with pytest.raises(sqlite3.OperationalError):
        retention.prune_session_events(failing, keep_days=30, ts="2026-05-30T12:00:00Z")

    # The event must still be present (the DELETE was rolled back, not committed).
    events = [r["title"] for r in conn.execute(
        "SELECT title FROM session_events").fetchall()]
    assert events == ["To be rolled up"], (
        "delete failure must roll back — events must survive"
    )
    # And no summary should have been committed for the session.
    row = conn.execute("SELECT summary FROM sessions WHERE id='s1'").fetchone()
    summary = row["summary"] if row is not None else None
    assert not summary or "To be rolled up" not in summary, (
        "roll-up summary must not be committed when the paired DELETE fails"
    )


# ---------------------------------------------------------------------------
# 3. Re-opening / migrate replay must not clobber protected memory rows.
# ---------------------------------------------------------------------------
def test_pinned_and_hardrule_memories_survive_open_replay_then_prune(tmp_path):
    """Re-running open_memory_db (idempotent migrate replay: db.py tolerates
    re-applied DDL/ADD COLUMN) on the same file, then pruning, leaves pinned +
    hard-rule memories intact."""
    now = "2026-05-30T12:00:00Z"
    path = str(tmp_path / "m.db")
    c1 = memory_lib.open_memory_db(path)
    _save(c1, id="m_pinned", type="user", ts=now, pinned=True)
    _save(c1, id="m_rule", type="hard-rule", ts=now, pinned=True)
    before = _memory_snapshot(c1)
    version_before = c1.execute("PRAGMA user_version").fetchone()[0]
    c1.close()

    # Idempotent re-open (re-runs db.migrate against the already-shaped file).
    c2 = memory_lib.open_memory_db(path)
    after_reopen = _memory_snapshot(c2)
    version_after = c2.execute("PRAGMA user_version").fetchone()[0]
    assert after_reopen == before, "migrate replay must not clobber memories"
    assert version_after == version_before, "schema version must be stable on replay"

    deleted = retention.prune_session_events(c2, keep_days=30, ts=now)
    assert deleted == 0  # no events to prune

    after = _memory_snapshot(c2)
    assert after == before, "reopen + prune must not clobber memories"
    assert after["m_pinned"][0] == 1
    assert after["m_rule"][2] == "hard-rule"
    c2.close()


# ---------------------------------------------------------------------------
# 4. Multi-session roll-up handles a session with no pre-existing sessions row.
# ---------------------------------------------------------------------------
def test_prune_multi_session_rollup_handles_missing_session_row(conn):
    """Two sessions with old events; prune must roll up BOTH into a summary and
    delete every old event — even for a session whose sessions row is absent at
    prune time (the ON CONFLICT INSERT branch, retention.py:32-35).

    record_session_event auto-creates the parent sessions row (FK), so we delete
    s2's row afterwards to reproduce the "missing sessions row at prune time"
    state.  Under foreign_keys=ON the orphaned event survives the parent delete
    only if there is no FK from session_events->sessions; if there is, the delete
    will fail and the test falls back to exercising the UPDATE branch for both —
    still a valid multi-session roll-up assertion.
    """
    memory_lib.record_session_event(conn, session_id="s1", kind="task_done",
                                    title="s1 old work", ts="2026-01-01T00:00:00Z")
    memory_lib.record_session_event(conn, session_id="s2", kind="task_done",
                                    title="s2 old work", ts="2026-01-02T00:00:00Z")

    # Try to force the INSERT branch by removing s2's parent row.
    try:
        conn.execute("DELETE FROM sessions WHERE id='s2'")
    except sqlite3.Error:
        pass  # FK prevents it -> both rows present, UPDATE branch exercised.

    deleted = retention.prune_session_events(conn, keep_days=30, ts="2026-05-30T12:00:00Z")

    assert deleted == 2, "all old events across both sessions must be deleted"
    remaining = conn.execute("SELECT COUNT(*) AS c FROM session_events").fetchone()["c"]
    assert remaining == 0

    s1 = conn.execute("SELECT summary FROM sessions WHERE id='s1'").fetchone()
    s2 = conn.execute("SELECT summary FROM sessions WHERE id='s2'").fetchone()
    assert s1 is not None and "s1 old work" in (s1["summary"] or "")
    assert s2 is not None and "s2 old work" in (s2["summary"] or ""), (
        "missing sessions row must be (re)created by the ON CONFLICT roll-up"
    )
