import json
from ultra_memory.hooks import checkpoint


def _write_transcript(tmp_path, events):
    p = tmp_path / "t.jsonl"
    with p.open("w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return p


def _tool_use(name, inp):
    return {"message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


def test_completed_tasks_basic(tmp_path):
    t = _write_transcript(tmp_path, [
        _tool_use("TaskCreate", {"tasks": [{"id": "t1", "subject": "Build hook"},
                                            {"id": "t2", "subject": "Wire it"}]}),
        _tool_use("TaskUpdate", {"task_id": "t1", "status": "in_progress"}),
        _tool_use("TaskUpdate", {"task_id": "t1", "status": "completed"}),
    ])
    assert checkpoint.completed_tasks(t) == [("t1", "Build hook")]


def test_completed_tasks_reopened_task_uses_final_status(tmp_path):
    t = _write_transcript(tmp_path, [
        _tool_use("TaskCreate", {"tasks": [{"id": "t1", "subject": "Flaky"}]}),
        _tool_use("TaskUpdate", {"task_id": "t1", "status": "completed"}),
        _tool_use("TaskUpdate", {"task_id": "t1", "status": "in_progress"}),  # re-opened
    ])
    assert checkpoint.completed_tasks(t) == []  # final status not completed


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
