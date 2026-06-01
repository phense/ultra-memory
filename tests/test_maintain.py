import datetime
import hashlib
import json

from ultra_memory import maintain, memory_lib


def test_maintain_module_exposes_run_and_main():
    assert hasattr(maintain, "run")
    assert hasattr(maintain, "main")


def _ts(s):
    return s  # already "...Z"


def test_run_prunes_and_exports_and_stamps(tmp_path):
    db = tmp_path / "m.db"
    conn = memory_lib.open_memory_db(str(db))
    memory_lib.record_session_event(conn, session_id="s1", kind="task_done",
                                    title="ancient", ts="2026-01-01T00:00:00Z")
    memory_lib.record_session_event(conn, session_id="s1", kind="task_done",
                                    title="recent", ts="2026-05-30T00:00:00Z")
    out = tmp_path / "export"
    res = maintain.run(conn, out_dir=str(out), ts="2026-05-31T12:00:00Z",
                       keep_days=90, force=True)
    assert res["pruned"] == 1                       # the 2026-01-01 event is > 90d old
    assert res["exported"] is True
    assert res["skipped"] is False
    remaining = [r[0] for r in conn.execute("SELECT title FROM session_events").fetchall()]
    assert remaining == ["recent"]
    last = conn.execute("SELECT value FROM meta WHERE key='last_maintenance'").fetchone()[0]
    assert last == "2026-05-31T12:00:00Z"
    conn.close()


def test_run_throttles_within_window(tmp_path):
    db = tmp_path / "m.db"
    conn = memory_lib.open_memory_db(str(db))
    out = tmp_path / "export"
    maintain.run(conn, out_dir=str(out), ts="2026-05-31T12:00:00Z", force=True)
    # 5h later, NOT forced -> throttled no-op.
    res = maintain.run(conn, out_dir=str(out), ts="2026-05-31T17:00:00Z", force=False)
    assert res["skipped"] is True
    assert res["pruned"] == 0


def test_run_proceeds_after_window(tmp_path):
    db = tmp_path / "m.db"
    conn = memory_lib.open_memory_db(str(db))
    out = tmp_path / "export"
    maintain.run(conn, out_dir=str(out), ts="2026-05-31T12:00:00Z", force=True)
    # 21h later (> _THROTTLE_HOURS=20), not forced -> runs.
    res = maintain.run(conn, out_dir=str(out), ts="2026-06-01T09:00:00Z", force=False)
    assert res["skipped"] is False
    conn.close()


def test_maintain_against_live_shaped_db(tmp_path):
    """Risk 11.3: prune against a NON-freshly-imported, multi-session, growing
    session_events table (the live shape), not a 2-row fixture."""
    from ultra_memory import setup
    db = tmp_path / "live.db"
    conn = memory_lib.open_memory_db(str(db))
    # Stamp it import-complete, like a real post-setup consumer.
    setup.mark_import_complete(str(db))
    # 3 sessions, 40 events each, ages spanning ~1 year (some > keep_days, some not).
    base = datetime.datetime(2026, 1, 1)
    for s in range(3):
        for i in range(40):
            day = base + datetime.timedelta(days=s * 100 + i)
            conn.execute("INSERT OR IGNORE INTO sessions (id) VALUES (?)", (f"sess{s}",))
            memory_lib.record_session_event(
                conn, session_id=f"sess{s}", kind="task_done",
                title=f"evt {s}-{i}", ts=day.strftime("%Y-%m-%dT%H:%M:%SZ"))
    total_before = conn.execute("SELECT COUNT(*) FROM session_events").fetchone()[0]
    assert total_before == 120
    out = tmp_path / "export"
    res = maintain.run(conn, out_dir=str(out), ts="2026-12-31T00:00:00Z",
                       keep_days=90, force=True)
    total_after = conn.execute("SELECT COUNT(*) FROM session_events").fetchone()[0]
    # Old rows pruned; recent ones (within 90d of 2026-12-31) retained.
    assert res["pruned"] > 0
    assert total_after == total_before - res["pruned"]
    # Pruned rows are archived into a session summary, not lost.
    summaries = conn.execute(
        "SELECT summary FROM sessions WHERE summary LIKE '%Archived events%'").fetchall()
    assert summaries
    # The export wrote real view files for an import-stamped DB.
    assert (out).exists()
    conn.close()


def _spool_record(spool_dir, rec):
    """Write a spool file the way memory_lib._spool does (content-hash name)."""
    payload = json.dumps(rec, sort_keys=True, ensure_ascii=False)
    key = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    spool_dir.mkdir(parents=True, exist_ok=True)
    (spool_dir / f"{key}.json").write_text(payload, encoding="utf-8")
    return key


def test_run_drains_write_spool_before_prune_export(tmp_path):
    """FIX 1: a durable spool casualty (a busy-time-lost record_session_event) is
    re-applied by maintain.run — the single serialized nightly drainer — and its
    spool file is removed, so a real live writer's spooled write is never lost."""
    db = tmp_path / "m.db"
    conn = memory_lib.open_memory_db(str(db))
    spool_dir = tmp_path / "memory_spool"
    # A spooled record_session_event intent: the row is NOT yet in the DB.
    _spool_record(spool_dir, {
        "op": "record_session_event", "session_id": "s-spooled",
        "kind": "skill_learning_candidate", "title": "spooled checkpoint",
        "ts": "2026-05-30T00:00:00Z", "detail": None, "files": None, "refs": None,
        "event_key": "ignored-recomputed", "outcome_signal": "tests_passed"})
    # RED precondition: the row is absent and the spool file exists.
    assert conn.execute(
        "SELECT 1 FROM session_events WHERE session_id='s-spooled'").fetchone() is None
    assert list(spool_dir.glob("*.json"))

    out = tmp_path / "export"
    maintain.run(conn, out_dir=str(out), ts="2026-05-31T12:00:00Z",
                 keep_days=90, force=True)

    # GREEN: the spooled row is now present and the spool file is drained.
    row = conn.execute(
        "SELECT outcome_signal FROM session_events WHERE session_id='s-spooled' "
        "AND kind='skill_learning_candidate'").fetchone()
    assert row is not None, "maintain.run did not replay the spooled write"
    assert row[0] == "tests_passed"
    assert not list(spool_dir.glob("*.json")), "spool file was not drained after replay"
    conn.close()


def test_run_replay_failure_is_fail_open(tmp_path, monkeypatch):
    """FIX 1: a replay_spool error must log + continue into prune/export, never
    abort maintain (fail-open, mirroring the wiki_sync seam)."""
    db = tmp_path / "m.db"
    conn = memory_lib.open_memory_db(str(db))
    # An old event so prune still has work to do after a replay blow-up.
    memory_lib.record_session_event(conn, session_id="s1", kind="task_done",
                                    title="ancient", ts="2026-01-01T00:00:00Z")

    def boom(*a, **k):
        raise RuntimeError("replay exploded")

    monkeypatch.setattr(maintain.memory_lib, "replay_spool", boom)
    out = tmp_path / "export"
    res = maintain.run(conn, out_dir=str(out), ts="2026-05-31T12:00:00Z",
                       keep_days=90, force=True)
    # Maintenance still completed its real work despite the replay error.
    assert res["pruned"] == 1
    assert res["exported"] is True
    assert res["skipped"] is False
    conn.close()


# ---------------------------------------------------------------------------
# FIX 2 — maintain._set_meta must route through the SAME bounded busy-retry as
# memory_lib._write_txn (a raw BEGIN IMMEDIATE used to raise 'database is locked'
# immediately on a transient SQLITE_BUSY).
# ---------------------------------------------------------------------------


def _lock_holder(db_path):
    import sqlite3
    holder = sqlite3.connect(str(db_path))
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(
        "INSERT INTO meta (key, value) VALUES ('lockprobe','1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    return holder


def test_set_meta_retries_one_busy_then_succeeds(tmp_path, monkeypatch):
    """FIX 2: a single transient SQLITE_BUSY on _set_meta's BEGIN IMMEDIATE is
    retried (after the lock releases during a backoff sleep) and the write SUCCEEDS,
    instead of raising 'database is locked'."""
    db_path = tmp_path / "m.db"
    conn = memory_lib.open_memory_db(str(db_path))
    conn.execute("PRAGMA busy_timeout=0")

    holder = _lock_holder(db_path)
    state = {"released": False}

    def release_then_sleep(_secs):
        if not state["released"]:
            holder.rollback()
            holder.close()
            state["released"] = True

    monkeypatch.setattr(memory_lib.time, "sleep", release_then_sleep)

    maintain._set_meta(conn, "some_key", "some_value")
    assert state["released"], "the retry path must have been exercised (a sleep fired)"
    assert conn.execute(
        "SELECT value FROM meta WHERE key='some_key'").fetchone()[0] == "some_value"
    conn.close()
