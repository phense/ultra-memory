"""Shared, fail-open helpers for the session hooks. No LLM, no writes."""
import json
import os
import sqlite3
from pathlib import Path

INTERACTIVE_SOURCES = {"startup", "resume", "clear", "compact"}


def agent_role_optout(payload=None):
    """True when this hook must no-op (cron/subagent/non-interactive run).

    Two signals (spec §10): an explicit env marker set by cron wrappers, or a
    SessionStart payload whose `source` is not an interactive session start.
    """
    if os.environ.get("ULTRA_MEMORY_AGENT_ROLE", "").strip():
        return True
    if payload is not None:
        source = payload.get("source")
        if source is not None and source not in INTERACTIVE_SOURCES:
            return True
    return False


def resolve_db_path(env=None):
    """Resolve the memory.db path the SAME way the knowledge MCP does, so the whole
    plugin is zero-config-consistent: explicit ``ULTRA_MEMORY_DB`` wins, else the
    fixed global ``~/.ultra-memory/memory.db`` — never cwd, never project-local.
    Delegates to the single engine resolver (``knowledge_mcp.db_path_from_env``).
    Returns a ``str`` (hooks feed it to ``db_ready`` / ``open_memory_db``)."""
    from ..knowledge_mcp import db_path_from_env
    return str(db_path_from_env(env if env is not None else os.environ))


def db_ready(db_path):
    """True only when the schema is present AND the one-time import is complete.

    Pre-import we fail-open to the legacy path (spec §7.4). Any error → not ready.
    """
    p = Path(db_path)
    if not p.is_file():
        return False
    try:
        conn = sqlite3.connect(str(p))
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='import_complete'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return bool(row) and str(row[0]) == "1"


def read_payload(stream):
    """Parse a hook stdin payload; return {} on any error (fail-open)."""
    try:
        return json.load(stream)
    except (json.JSONDecodeError, ValueError):
        return {}


def session_id_of(payload, transcript_path=None):
    """Prefer the payload session_id; fall back to the transcript filename stem."""
    sid = (payload or {}).get("session_id")
    if sid:
        return str(sid)
    if transcript_path:
        return Path(transcript_path).stem
    return "unknown-session"
