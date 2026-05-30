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
