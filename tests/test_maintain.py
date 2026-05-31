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

