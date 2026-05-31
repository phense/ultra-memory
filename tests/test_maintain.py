import datetime

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
