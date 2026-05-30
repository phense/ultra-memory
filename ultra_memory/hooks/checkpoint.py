"""Stop-hook checkpoint: record completed tasks from the raw transcript JSONL.

Pure replay functions (no I/O beyond reading the transcript file) live here and
are unit-tested. `run()` wires them to memory_lib. NEVER blocks the session.
"""
import json
from pathlib import Path

_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}


def _tool_uses(transcript_path):
    """Yield (name, input) for every tool_use block, tolerant of bad lines."""
    p = Path(transcript_path)
    try:
        with p.open("r", encoding="utf-8") as fh:
            for raw in fh:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                content = event.get("message", {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        yield block.get("name", ""), (block.get("input") or {})
    except OSError:
        return


def completed_tasks(transcript_path):
    """Replay TaskCreate/TaskUpdate → list of (task_id, subject) finally completed.

    Preserves first-seen order of task_ids (spec §9.1).
    """
    subjects = {}      # task_id -> subject
    order = []         # task_id creation order
    final_status = {}  # task_id -> last status seen
    for name, inp in _tool_uses(transcript_path):
        if name == "TaskCreate":
            for task in inp.get("tasks", []) or []:
                tid = str(task.get("id", ""))
                if not tid:
                    continue
                if tid not in subjects:
                    order.append(tid)
                subjects[tid] = str(task.get("subject", task.get("description", "")))
        elif name == "TaskUpdate":
            tid = str(inp.get("task_id", ""))
            if tid:
                final_status[tid] = str(inp.get("status", ""))
    return [(tid, subjects[tid]) for tid in order
            if final_status.get(tid) == "completed" and tid in subjects]


def has_material_work(transcript_path):
    """True if the session edited files or committed — i.e. worth checkpointing."""
    for name, inp in _tool_uses(transcript_path):
        if name in _EDIT_TOOLS:
            return True
        if name == "Bash" and "git commit" in str(inp.get("command", "")):
            return True
    return False
