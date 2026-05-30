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


from ultra_memory import memory_lib
from ultra_memory.hooks import common


def run(payload, *, db_path, ts):
    """Record completed tasks as session_events. Returns {} (never blocks).

    Fail-open at every gate: recursion guard, role, db-readiness, missing
    transcript, and any unexpected error are all swallowed → {} (allow stop).
    """
    try:
        if payload.get("stop_hook_active"):
            return {}
        if common.agent_role_optout(payload):
            return {}
        if not common.db_ready(db_path):
            return {}
        transcript = payload.get("transcript_path")
        if not transcript or not Path(transcript).is_file():
            return {}
        tasks = completed_tasks(transcript)
        if not tasks and not has_material_work(transcript):
            return {}
        session_id = common.session_id_of(payload, transcript)
        conn = memory_lib.open_memory_db(str(db_path))
        try:
            for tid, subject in tasks:
                memory_lib.record_session_event(
                    conn, session_id=session_id, kind="task_done",
                    title=subject or tid, ts=ts, detail=None,
                )
        finally:
            conn.close()
    except Exception:
        # Hooks must never block the session on error.
        return {}
    return {}


def _db_path_from_env():
    import os
    return os.environ.get("ULTRA_MEMORY_DB", "")


def main(stdin, stdout):
    import datetime
    payload = common.read_payload(stdin)
    db_path = _db_path_from_env()
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = run(payload, db_path=db_path, ts=ts)
    if out:
        json.dump(out, stdout)
    return 0
