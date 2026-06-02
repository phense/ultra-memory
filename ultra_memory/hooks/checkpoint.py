"""Stop-hook checkpoint: record completed tasks from the raw transcript JSONL.

Pure replay functions (no I/O beyond reading the transcript file) live here and
are unit-tested. `run()` wires them to memory_lib. NEVER blocks the session.

Real transcript shapes (verified against live transcripts, not assumed):
- TaskCreate `tool_use.input` = {"subject", "description", "activeForm"} — ONE
  task per call, with NO task id. The assigned id only appears in the matching
  `tool_result` content: "Task #<N> created successfully: <subject>".
- TaskUpdate `tool_use.input` = {"taskId": "<N>", "status"?, "addBlockedBy"?, ...}
  — camelCase `taskId`; `status` is optional (a blockedBy-only update has none).
So we recover id→subject from tool_result text and fold status from TaskUpdate.
"""
import json
import os
import re
from pathlib import Path

from ultra_memory import memory_lib
from ultra_memory.hooks import common
from ultra_memory.maintenance import session_ingest

_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}
_CREATE_RE = re.compile(r"^Task #(\d+) created successfully:\s*(.+)$")


def _blocks(transcript_path):
    """Yield every content block dict, tolerant of bad/non-list lines."""
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
                    if isinstance(block, dict):
                        yield block
    except OSError:
        return


def _result_text(block):
    """tool_result content is either a string or a list of {type:text,text:..}."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def completed_tasks(transcript_path):
    """Replay the transcript → list of (task_id, subject) finally completed.

    id→subject comes from TaskCreate tool_result text; status from TaskUpdate
    (only when an explicit `status` is present, so a blockedBy-only update never
    clears a prior completed status). Insertion order = creation order.
    """
    subjects = {}      # task_id(str) -> subject, insertion-ordered
    final_status = {}  # task_id -> last explicit status
    for block in _blocks(transcript_path):
        btype = block.get("type")
        if btype == "tool_result":
            m = _CREATE_RE.match(_result_text(block).strip())
            if m:
                tid, subject = m.group(1), m.group(2).strip()
                subjects.setdefault(tid, subject)
        elif btype == "tool_use" and block.get("name") == "TaskUpdate":
            inp = block.get("input") or {}
            tid = str(inp.get("taskId", ""))
            if tid and "status" in inp:
                final_status[tid] = str(inp.get("status", ""))
    return [(tid, subjects[tid]) for tid in subjects
            if final_status.get(tid) == "completed"]


def has_material_work(transcript_path):
    """True if the session edited files or committed — i.e. worth checkpointing."""
    for block in _blocks(transcript_path):
        if block.get("type") != "tool_use":
            continue
        name = block.get("name", "")
        inp = block.get("input") or {}
        if name in _EDIT_TOOLS:
            return True
        if name == "Bash" and "git commit" in str(inp.get("command", "")):
            return True
    return False


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
            # Subsystem 4: enqueue this finished session for the throttled ingestion
            # pass (mine the transcript for durable knowledge + corrections). Gated by
            # SESSION_INGEST_ENABLE (default OFF → a no-op), fail-open — never blocks.
            session_ingest.enqueue_if_enabled(
                conn, session_id=session_id, transcript_path=str(transcript),
                ts=ts, env=os.environ)
        finally:
            conn.close()
    except Exception:
        # Hooks must never block the session on error.
        return {}
    return {}


def _db_path_from_env():
    # Zero-config-consistent with the knowledge MCP: explicit ULTRA_MEMORY_DB wins,
    # else the fixed global ~/.ultra-knowledge/memory.db (never cwd, never project-local).
    return common.resolve_db_path()


def main(stdin, stdout):
    import datetime
    payload = common.read_payload(stdin)
    db_path = _db_path_from_env()
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = run(payload, db_path=db_path, ts=ts)
    if out:
        json.dump(out, stdout)
    return 0
