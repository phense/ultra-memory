"""SessionStart-hook rehydration: a budgeted, DB-derived gist. No LLM, read-only."""
import json
from pathlib import Path

from ultra_memory.hooks import common

_PULL_POINTER = (
    "Pull more on demand: query the memory layer (memory_query / the knowledge MCP) "
    "or open the named file under the harness memory dir."
)


def _knowledge_pin_lines(conn, *, limit=12):
    """Render the pinned KNOWLEDGE rows (SP-3 Stage 4, D7) as gist lines.

    Reads `knowledge_pins WHERE pinned=1`; the human-readable display title comes
    from the `unified_index` mirror (Stage 5) and falls back to the slug when no
    mirror row exists yet, so a pin is never silently dropped. Returns `[]` when
    there are no pinned knowledge rows — this is the byte-identity guarantee: an
    empty `knowledge_pins` (Trading's current state) appends nothing to the gist.
    The `unified_index` lookup is LEFT JOIN-tolerant via a guarded subquery so a
    pre-Stage-5 DB (no unified_index content) still renders the slug."""
    try:
        rows = conn.execute(
            "SELECT k.slug, "
            "       (SELECT u.title FROM unified_index u WHERE u.slug = k.slug) AS title "
            "FROM knowledge_pins k WHERE k.pinned=1 "
            "ORDER BY k.pinned_at DESC, k.slug ASC"
        ).fetchall()
    except Exception:
        # Fail-open: a malformed/absent table never breaks rehydration.
        return []
    lines = []
    for slug, title in rows[:limit]:
        label = (title or "").strip() or slug
        lines.append(f"- {label}")
    return lines


def build_gist(conn, *, budget_chars=2000):
    """Compose the rehydration gist from the DB. Each section is capped so one
    section can't starve the others; the whole is truncated to budget_chars."""
    sections = []

    # One pin space (SP-3 Stage 4, D7): union memory pins (memories.pinned) with
    # knowledge pins (knowledge_pins, migration 0004) into the SINGLE "## Pinned
    # rules" section. SAFETY INVARIANT for the live merge to ultra-memory master
    # (Trading's SessionStart hook runs this): with ZERO knowledge_pins rows —
    # Trading's current state — the output is byte-identical to the memory-only
    # gist. The knowledge block is appended ONLY when there is at least one pinned
    # knowledge row, so an empty knowledge_pins table changes nothing.
    pinned = conn.execute(
        "SELECT title, body FROM memories WHERE pinned=1 AND status='active' "
        "ORDER BY updated_at DESC"
    ).fetchall()
    knowledge_pins = _knowledge_pin_lines(conn)
    if pinned or knowledge_pins:
        lines = []
        for t, b in pinned:
            first = (b or "").strip().splitlines()
            head = first[0][:160] if first else ""
            lines.append(f"- {t}: {head}")
        # Memory pins capped at 12 (unchanged); knowledge pins append after, so the
        # byte-identity hold when knowledge_pins is empty (no trailing change).
        lines = lines[:12] + knowledge_pins
        sections.append("## Pinned rules\n" + "\n".join(lines))

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


from ultra_memory import memory_lib


def run(payload, *, db_path, shadow, ts, shadow_out=None, budget_chars=2000):
    """Build + inject the gist (live) or log it (shadow). Returns {} when no
    injection. Fail-open: any error → {} (SessionStart proceeds without us)."""
    try:
        if common.agent_role_optout(payload):
            return {}
        if not common.db_ready(db_path):
            return {}
        conn = memory_lib.open_memory_db(str(db_path))
        try:
            gist = build_gist(conn, budget_chars=budget_chars)
        finally:
            conn.close()
        if not gist.strip():
            return {}
        if shadow:
            if shadow_out is not None:
                out_path = Path(shadow_out)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(f"<!-- shadow rehydration {ts} -->\n{gist}\n",
                                    encoding="utf-8")
            return {}
        return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                       "additionalContext": gist}}
    except Exception:
        return {}


def _budget_from_env():
    """Resolve the gist char budget from env (consumer-tunable); default 2000.

    Invalid / non-numeric values fail-soft back to the default so a bad config
    can never break rehydration."""
    import os
    raw = os.environ.get("ULTRA_MEMORY_REHYDRATE_BUDGET", "").strip()
    if not raw:
        return 2000
    try:
        val = int(raw)
    except ValueError:
        return 2000
    return val if val > 0 else 2000


def main(stdin, stdout):
    import datetime
    import os
    payload = common.read_payload(stdin)
    db_path = os.environ.get("ULTRA_MEMORY_DB", "")
    shadow = os.environ.get("ULTRA_MEMORY_SHADOW", "1") == "1"
    shadow_out = os.environ.get("ULTRA_MEMORY_SHADOW_OUT") or None
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = run(payload, db_path=db_path, shadow=shadow, ts=ts, shadow_out=shadow_out,
              budget_chars=_budget_from_env())
    if out:
        json.dump(out, stdout)
    return 0
