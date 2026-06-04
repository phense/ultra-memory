import json
from ultra_memory.hooks import checkpoint
from ultra_memory import memory_lib


def _write_transcript(tmp_path, events):
    p = tmp_path / "t.jsonl"
    with p.open("w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return p


def _tool_use(name, inp):
    return {"message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


def _create_result(task_id, subject):
    # Mirrors the real TaskCreate tool_result content string.
    return {"message": {"content": [{
        "type": "tool_result", "tool_use_id": f"toolu_{task_id}",
        "content": f"Task #{task_id} created successfully: {subject}"}]}}


def _task_update(task_id, **inp):
    inp = {"taskId": str(task_id), **inp}
    return _tool_use("TaskUpdate", inp)


def test_completed_tasks_basic(tmp_path):
    t = _write_transcript(tmp_path, [
        _create_result(1, "Build hook"),
        _create_result(2, "Wire it"),
        _task_update(1, status="in_progress"),
        _task_update(1, status="completed"),
    ])
    assert checkpoint.completed_tasks(t) == [("1", "Build hook")]


def test_completed_tasks_reopened_task_uses_final_status(tmp_path):
    t = _write_transcript(tmp_path, [
        _create_result(1, "Flaky"),
        _task_update(1, status="completed"),
        _task_update(1, status="in_progress"),  # re-opened
    ])
    assert checkpoint.completed_tasks(t) == []  # final status not completed


def test_status_absent_update_does_not_clear_completed(tmp_path):
    # A blockedBy-only TaskUpdate (no status) must NOT wipe a prior completion.
    t = _write_transcript(tmp_path, [
        _create_result(1, "Done thing"),
        _task_update(1, status="completed"),
        _task_update(1, addBlockedBy=["2"]),  # no status key
    ])
    assert checkpoint.completed_tasks(t) == [("1", "Done thing")]


def test_completed_tasks_preserves_creation_order(tmp_path):
    t = _write_transcript(tmp_path, [
        _create_result(1, "First"),
        _create_result(2, "Second"),
        _task_update(2, status="completed"),
        _task_update(1, status="completed"),
    ])
    assert checkpoint.completed_tasks(t) == [("1", "First"), ("2", "Second")]


def test_completed_tasks_ignores_non_tool_lines(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("not json\n" + json.dumps(_tool_use("Read", {"file_path": "x"})) + "\n")
    assert checkpoint.completed_tasks(t) == []


def test_has_material_work_true_on_edit(tmp_path):
    t = _write_transcript(tmp_path, [_tool_use("Edit", {"file_path": "a.py"})])
    assert checkpoint.has_material_work(t) is True


def test_has_material_work_true_on_git_commit(tmp_path):
    t = _write_transcript(tmp_path, [_tool_use("Bash", {"command": "git commit -m x"})])
    assert checkpoint.has_material_work(t) is True


def test_has_material_work_false_on_reads_only(tmp_path):
    t = _write_transcript(tmp_path, [_tool_use("Read", {"file_path": "a.py"}),
                                      _tool_use("Bash", {"command": "ls"})])
    assert checkpoint.has_material_work(t) is False


def _ready_db(tmp_path):
    p = tmp_path / "memory.db"
    conn = memory_lib.open_memory_db(str(p))
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('import_complete','1')")
    conn.commit()
    conn.close()
    return p


def test_run_records_completed_tasks(tmp_path):
    t = _write_transcript(tmp_path, [
        _create_result(1, "Build hook"),
        _task_update(1, status="completed"),
    ])
    db_path = _ready_db(tmp_path)
    out = checkpoint.run({"session_id": "sess-1", "transcript_path": str(t)},
                         db_path=db_path, ts="2026-05-30T16:00:00Z")
    assert out == {}  # never blocks
    conn = memory_lib.open_memory_db(str(db_path))
    rows = conn.execute(
        "SELECT kind, title FROM session_events WHERE session_id='sess-1'"
    ).fetchall()
    conn.close()
    assert ("task_done", "Build hook") in [(r[0], r[1]) for r in rows]


def test_run_is_idempotent(tmp_path, monkeypatch):
    # Isolate the checkpoint task_done event_key dedup from the now-default-ON
    # session_ingest enqueue (explicit opt-out keeps the count at the task_done row).
    monkeypatch.setenv("SESSION_INGEST_ENABLE", "0")
    t = _write_transcript(tmp_path, [
        _create_result(1, "Build hook"),
        _task_update(1, status="completed"),
    ])
    db_path = _ready_db(tmp_path)
    args = ({"session_id": "sess-1", "transcript_path": str(t)},)
    checkpoint.run(*args, db_path=db_path, ts="2026-05-30T16:00:00Z")
    checkpoint.run(*args, db_path=db_path, ts="2026-05-30T16:00:00Z")  # re-run
    conn = memory_lib.open_memory_db(str(db_path))
    n = conn.execute(
        "SELECT COUNT(*) FROM session_events WHERE session_id='sess-1'"
    ).fetchone()[0]
    conn.close()
    assert n == 1  # event_key dedup


def test_run_noops_when_db_not_ready(tmp_path):
    t = _write_transcript(tmp_path, [
        _create_result(1, "X"),
        _task_update(1, status="completed"),
    ])
    out = checkpoint.run({"session_id": "s", "transcript_path": str(t)},
                         db_path=tmp_path / "absent.db", ts="2026-05-30T16:00:00Z")
    assert out == {}


def test_run_noops_when_stop_hook_active(tmp_path):
    db_path = _ready_db(tmp_path)
    out = checkpoint.run({"session_id": "s", "transcript_path": "x", "stop_hook_active": True},
                         db_path=db_path, ts="2026-05-30T16:00:00Z")
    assert out == {}
