"""SessionStart-hook rehydration: a budgeted, DB-derived gist. No LLM, read-only."""
import json
from pathlib import Path

from ultra_memory.hooks import common

_PULL_POINTER = (
    "Pull more on demand: query the memory layer (memory_query / the knowledge MCP) "
    "or open the named file under the harness memory dir."
)


def build_gist(conn, *, budget_chars=2000):
    """Compose the rehydration gist from the DB. Each section is capped so one
    section can't starve the others; the whole is truncated to budget_chars."""
    sections = []

    pinned = conn.execute(
        "SELECT title, body FROM memories WHERE pinned=1 AND status='active' "
        "ORDER BY updated_at DESC"
    ).fetchall()
    if pinned:
        lines = []
        for t, b in pinned:
            first = (b or "").strip().splitlines()
            head = first[0][:160] if first else ""
            lines.append(f"- {t}: {head}")
        sections.append("## Pinned rules\n" + "\n".join(lines[:12]))

    last = conn.execute(
        "SELECT summary FROM sessions WHERE summary IS NOT NULL AND summary != '' "
        "ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if last:
        sections.append("## Where we left off\n" + last[0][:500])
    else:
        recent = conn.execute(
            "SELECT kind, title FROM session_events ORDER BY ts DESC LIMIT 6"
        ).fetchall()
        if recent:
            sections.append("## Recent activity\n" +
                            "\n".join(f"- [{k}] {t}" for k, t in recent))

    followups = conn.execute(
        "SELECT title FROM session_events WHERE kind='followup' AND resolved=0 "
        "ORDER BY ts DESC LIMIT 10"
    ).fetchall()
    if followups:
        sections.append("## Open follow-ups\n" +
                        "\n".join(f"- {r[0]}" for r in followups))

    hot = conn.execute(
        "SELECT title FROM memories WHERE status='active' "
        "ORDER BY access_count DESC, updated_at DESC LIMIT 10"
    ).fetchall()
    if hot:
        sections.append("## Hot memories\n" + "\n".join(f"- {r[0]}" for r in hot))

    sections.append(_PULL_POINTER)
    gist = "\n\n".join(sections)
    if len(gist) > budget_chars:
        gist = gist[:budget_chars].rsplit("\n", 1)[0] + "\n…(truncated)"
    return gist
