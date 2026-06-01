"""SessionStart-hook rehydration: a budgeted, DB-derived gist. No LLM, read-only."""
import json
from pathlib import Path

from ultra_memory.hooks import common

_PULL_POINTER = (
    "Pull more on demand: query the memory layer (memory_query / the knowledge MCP) "
    "or open the named file under the harness memory dir."
)

# Hard cap on the length of any single rendered title/summary/label. The gist is
# handed to the LLM verbatim as additionalContext, so a pathological title can't
# be allowed to dominate the budget either.
_FIELD_MAX = 200
# Per-section line caps (unchanged behavior; surfaced as constants so the FIX-2
# pinned-survival logic and the omitted-count marker share one source of truth).
_PIN_MEM_CAP = 12


def _one_line(s, *, limit=_FIELD_MAX):
    """Collapse a field to a single safe gist line.

    The gist is injected verbatim into the trusted SessionStart context, so any
    field rendered into it MUST NOT be able to forge a section header (`## ...`)
    or a list item (`- ...`) on its own line (FIX 1 — gist-structure injection
    via an embedded newline in a title/summary/follow-up; save_memory does not
    strip newlines from titles). `" ".join(s.split())` collapses every run of
    whitespace — including newlines, tabs, and other control whitespace — onto a
    single line, so the only structural lines in the gist are the ones build_gist
    itself emits. Length is capped so one field can't dominate the budget."""
    flat = " ".join((s or "").split())
    return flat[:limit]


def _knowledge_pin_lines(conn, *, limit=12):
    """Render the pinned KNOWLEDGE rows (SP-3 Stage 4, D7) as gist lines.

    Reads `knowledge_pins WHERE pinned=1`, INNER-joined to the `unified_index`
    mirror (Stage 5): a pin is rendered ONLY when its page still exists. FIX 4 —
    nothing reconciles knowledge_pins when a page is deleted, so a stale pin whose
    page is gone would otherwise emit a bare-slug "rule" forever; the INNER JOIN
    (via `WHERE EXISTS`) skips it. The slug-fallback title is KEPT for a page that
    EXISTS but has an empty title (`u.title` NULL/blank → slug); it is only the
    page-row-absent case that is skipped. Returns `[]` when there are no surviving
    pinned knowledge rows — the byte-identity guarantee for an empty / fully-stale
    knowledge_pins (Trading's current state appends nothing)."""
    try:
        rows = conn.execute(
            "SELECT k.slug, "
            "       (SELECT u.title FROM unified_index u WHERE u.slug = k.slug) AS title "
            "FROM knowledge_pins k WHERE k.pinned=1 "
            "  AND EXISTS (SELECT 1 FROM unified_index u WHERE u.slug = k.slug) "
            "ORDER BY k.pinned_at DESC, k.slug ASC"
        ).fetchall()
    except Exception:
        # Fail-open: a malformed/absent table never breaks rehydration.
        return []
    lines = []
    for slug, title in rows[:limit]:
        label = _one_line((title or "").strip() or slug)
        lines.append(f"- {label}")
    return lines


def build_gist(conn, *, budget_chars=2000):
    """Compose the rehydration gist from the DB.

    The safety-critical "## Pinned rules" section (German tax fence, OAuth-only,
    …) is rendered FIRST and is EXEMPT from the budget tail-cut (FIX 2): under
    budget pressure a later section is dropped before any pinned rule, and a
    pinned rule that the cap forces out is named with an explicit
    "(N more pinned rules omitted)" marker rather than silently lost. Every field
    rendered into the gist is passed through `_one_line` so it can't forge gist
    structure (FIX 1)."""
    # --- Pinned rules (rendered first; budget-exempt) -----------------------
    # One pin space (SP-3 Stage 4, D7): union memory pins (memories.pinned) with
    # knowledge pins (knowledge_pins, migration 0004) into the SINGLE "## Pinned
    # rules" section. SAFETY INVARIANT for the live merge to ultra-memory master
    # (Trading's SessionStart hook runs this): with ZERO surviving knowledge_pins
    # rows — Trading's current state — the output is byte-identical to the
    # memory-only gist. The knowledge block is appended ONLY when there is at
    # least one surviving pinned knowledge row.
    # FIX 5: stable secondary `id` tie-break so equal-`updated_at` pins (e.g. a
    # bootstrap import stamps same-mtime files identically) sort deterministically.
    pinned = conn.execute(
        "SELECT title, body FROM memories WHERE pinned=1 AND status='active' "
        "ORDER BY updated_at DESC, id"
    ).fetchall()
    knowledge_pins = _knowledge_pin_lines(conn)
    pinned_section = None
    if pinned or knowledge_pins:
        mem_lines = []
        for t, b in pinned:
            first = (b or "").strip().splitlines()
            head = _one_line(first[0]) if first else ""
            mem_lines.append(f"- {_one_line(t)}: {head}")
        # Memory pins capped at _PIN_MEM_CAP (unchanged); knowledge pins append
        # after — so byte-identity holds when knowledge_pins is empty.
        dropped = max(0, len(mem_lines) - _PIN_MEM_CAP)
        lines = mem_lines[:_PIN_MEM_CAP] + knowledge_pins
        if dropped:
            # Never lose a pinned rule silently — name the count instead (FIX 2).
            lines.append(f"- (…{dropped} more pinned rules omitted)")
        pinned_section = "## Pinned rules\n" + "\n".join(lines)

    # --- Later sections (these bear the budget tail-cut) --------------------
    sections = []
    last = conn.execute(
        "SELECT summary FROM sessions WHERE summary IS NOT NULL AND summary != '' "
        "ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if last:
        sections.append("## Where we left off\n" + _one_line(last[0], limit=500))
    else:
        recent = conn.execute(
            "SELECT kind, title FROM session_events ORDER BY ts DESC LIMIT 6"
        ).fetchall()
        if recent:
            sections.append("## Recent activity\n" +
                            "\n".join(f"- [{_one_line(k)}] {_one_line(t)}"
                                      for k, t in recent))

    followups = conn.execute(
        "SELECT title FROM session_events WHERE kind='followup' AND resolved=0 "
        "ORDER BY ts DESC LIMIT 10"
    ).fetchall()
    if followups:
        sections.append("## Open follow-ups\n" +
                        "\n".join(f"- {_one_line(r[0])}" for r in followups))

    # FIX 3: exclude pinned memories from Hot memories — a pinned unit already
    # appears in "## Pinned rules"; re-listing it wastes budget and double-counts.
    # FIX 5: id tie-break for deterministic ordering of equal-(access_count,
    # updated_at) rows.
    hot = conn.execute(
        "SELECT title FROM memories WHERE status='active' AND pinned=0 "
        "ORDER BY access_count DESC, updated_at DESC, id LIMIT 10"
    ).fetchall()
    if hot:
        sections.append("## Hot memories\n" +
                        "\n".join(f"- {_one_line(r[0])}" for r in hot))

    sections.append(_PULL_POINTER)
    later = "\n\n".join(sections)

    # --- Assemble, applying the tail-cut ONLY to the later sections ---------
    if pinned_section is None:
        gist = later
        if len(gist) > budget_chars:
            gist = gist[:budget_chars].rsplit("\n", 1)[0] + "\n…(truncated)"
        return gist

    # The pinned section is guaranteed in full. The remaining budget (if any) is
    # spent on the later sections; if the pinned section alone meets/exceeds the
    # budget, the later sections are dropped entirely (never the reverse).
    remaining = budget_chars - len(pinned_section) - 2  # 2 for the "\n\n" joiner
    if remaining <= 0:
        return pinned_section
    if len(later) <= remaining:
        return pinned_section + "\n\n" + later
    trimmed = later[:remaining].rsplit("\n", 1)[0] + "\n…(truncated)"
    return pinned_section + "\n\n" + trimmed


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
    # Zero-config-consistent with the knowledge MCP: explicit ULTRA_MEMORY_DB wins,
    # else <CLAUDE_PROJECT_DIR>/data/memory.db, else ~/.claude/memory.db (never cwd).
    db_path = common.resolve_db_path()
    shadow = os.environ.get("ULTRA_MEMORY_SHADOW", "1") == "1"
    shadow_out = os.environ.get("ULTRA_MEMORY_SHADOW_OUT") or None
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = run(payload, db_path=db_path, shadow=shadow, ts=ts, shadow_out=shadow_out,
              budget_chars=_budget_from_env())
    if out:
        json.dump(out, stdout)
    return 0
