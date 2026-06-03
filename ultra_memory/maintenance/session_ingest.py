"""Subsystem 4 — the session-as-ingestion-stream (north-star 2026-06-01 §4).

*The session itself is an ingestion source.* Every Claude Code session's transcript
is mined — once, by a throttled OAuth pass — for durable knowledge (→ the memory
store) and for explicit user corrections (→ the Tier-1 correction fast-path). This
module is split capture-fast / process-slow, mirroring the skill-learning loop:

  • slice 4a (THIS file's deterministic core, NO LLM): the session-ingest QUEUE
    (`session_events` rows, kind='session_ingest_pending', drained `resolved=0`) +
    the transcript DIGEST builder (a compact, tool-output-free view the LLM pass
    reads). The enqueue is gated by `SESSION_INGEST_ENABLE` — default OFF, a no-op,
    byte-identical behavior until armed (the north-star ships-active posture flip is
    Peter's explicit step on a real consumer).
  • slice 4b (the drain beat): one OAuth `claude` call per pending session →
    {extracted_knowledge, correction_detected, correction} → route. Built on top of
    this substrate.

Privacy (north-star §9): the digest EXCLUDES raw tool_result bodies (the secret +
size surface) — only the user/assistant prose and the tool NAMES are kept; and the
raw transcript is never persisted (only the extracted, redacted knowledge is, via
the `save_memory` strip_secrets chokepoint). Fail-open throughout.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from ultra_memory import memory_lib
from ultra_memory.claude_cli import run_claude  # the OAuth chokepoint (injectable runner)
from ultra_memory.maintenance.parse_utils import strip_json_fence

PENDING_KIND = "session_ingest_pending"
ENABLE_ENV = "SESSION_INGEST_ENABLE"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_FACTS = 8
DEFAULT_MAX_SKILL_LEARNINGS = 8   # per-session cap on skill-tagged learnings
DEFAULT_LIMIT = 10          # max pending sessions drained per pass
DEFAULT_TIMEOUT = 300       # one OAuth call per session


# --------------------------------------------------------------------------- #
# The queue (capture-fast).
# --------------------------------------------------------------------------- #

def _enabled(env) -> bool:
    return str((env or {}).get(ENABLE_ENV, "")).strip() not in ("", "0", "false", "False")


def enqueue(conn, *, session_id: str, transcript_path: str, ts: str) -> None:
    """Record one pending-ingest marker for a finished session. Idempotent per
    (session_id, ts) via the `session_events` event_key; the transcript path rides
    in `detail` so the drain pass can read it later (the file persists on disk)."""
    memory_lib.record_session_event(
        conn, session_id=session_id, kind=PENDING_KIND,
        title=session_id, detail=str(transcript_path), ts=ts)


def enqueue_if_enabled(conn, *, session_id, transcript_path, ts, env) -> bool:
    """Gated enqueue for the Stop hook: a no-op unless SESSION_INGEST_ENABLE is set
    (so default behavior is byte-identical). Fail-open — never raises into the hook.
    Returns True iff a marker was written."""
    if not _enabled(env):
        return False
    try:
        enqueue(conn, session_id=session_id, transcript_path=transcript_path, ts=ts)
        return True
    except Exception:
        return False


def pending_sessions(conn, *, limit: int = 20) -> list[dict]:
    """Up to `limit` un-resolved pending-ingest markers, oldest first. Each:
    {event_id, session_id, ts, transcript_path}."""
    rows = conn.execute(
        "SELECT id, session_id, ts, detail FROM session_events "
        "WHERE kind=? AND resolved=0 ORDER BY id LIMIT ?",
        (PENDING_KIND, int(limit)),
    ).fetchall()
    return [{"event_id": r["id"], "session_id": r["session_id"], "ts": r["ts"],
             "transcript_path": r["detail"]} for r in rows]


def mark_resolved(conn, *, event_id) -> None:
    """Mark a drained pending marker resolved=1 (idempotent; never deleted)."""
    def work():
        conn.execute("UPDATE session_events SET resolved=1 WHERE id=?", (event_id,))
    memory_lib._with_immediate_retry(conn, work)


# --------------------------------------------------------------------------- #
# Skill-candidate bridge (the consolidate-feeder supersession).
# --------------------------------------------------------------------------- #

_SKILL_CANDIDATE_KIND = "skill_learning_candidate"


def _skill_of(title: str) -> str:
    """The skill tag is the prefix before the first ':' (the Stop-hook title format
    '<skill>: skill invoked, ...')."""
    return (title or "").split(":", 1)[0].strip()


def skills_used_for(conn, session_id: str) -> set:
    """Distinct tracked-skill tags this session used, from its un-resolved
    skill_learning_candidate markers. Empty set if none — grounds skill_learnings."""
    rows = conn.execute(
        "SELECT title FROM session_events WHERE session_id=? AND kind=? AND resolved=0",
        (session_id, _SKILL_CANDIDATE_KIND)).fetchall()
    return {s for s in (_skill_of(r["title"]) for r in rows) if s}


def resolve_skill_candidates(conn, session_id: str) -> int:
    """Mark this session's skill_learning_candidate markers resolved=1 — the supersession
    of the thin consolidate feeder (the ingest pass already mined the content). Idempotent;
    never deletes (only the flag flips). Returns rows affected."""
    def work():
        cur = conn.execute(
            "UPDATE session_events SET resolved=1 WHERE session_id=? AND kind=? AND resolved=0",
            (session_id, _SKILL_CANDIDATE_KIND))
        return cur.rowcount
    return memory_lib._with_immediate_retry(conn, work)


# --------------------------------------------------------------------------- #
# The transcript digest (deterministic, no-LLM, tool-output-free).
# --------------------------------------------------------------------------- #

def _events(transcript_path):
    """Yield parsed transcript events, tolerant of bad lines / a missing file."""
    p = Path(transcript_path)
    try:
        with p.open("r", encoding="utf-8") as fh:
            for raw in fh:
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def build_digest(transcript_path, *, max_chars: int = 12000,
                 per_block: int = 800) -> str:
    """A compact text view of a session for the LLM ingestion pass: user + assistant
    PROSE plus the NAMES of tools used — but NOT tool_result bodies (excluded for
    size + as a secret surface; only the extracted knowledge, redacted at save, ever
    persists). Each prose block is truncated to `per_block`; the whole digest to
    `max_chars`. Deterministic; fail-open to "" on a missing/unreadable file."""
    lines: list[str] = []
    for ev in _events(transcript_path):
        msg = ev.get("message") or {}
        role = msg.get("role", "?")
        content = msg.get("content")
        if isinstance(content, str):
            txt = content.strip()
            if txt:
                lines.append(f"[{role}] {txt[:per_block]}")
            continue
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "text":
                txt = (b.get("text") or "").strip()
                if txt:
                    lines.append(f"[{role}] {txt[:per_block]}")
            elif btype == "tool_use":
                lines.append(f"[{role} ⚙ {b.get('name', 'tool')}]")
            # tool_result bodies are deliberately skipped (size + secret surface).
    return "\n".join(lines)[:max_chars]


# --------------------------------------------------------------------------- #
# The OAuth drain pass (process-slow) — slice 4b.
# --------------------------------------------------------------------------- #

def build_sys() -> str:
    return (
        "You mine ONE Claude Code session's transcript digest for DURABLE knowledge "
        "and EXPLICIT user corrections. Output STRICT JSON only.\n"
        "extracted_knowledge: only facts/preferences/decisions worth remembering "
        "across sessions — NOT transient task narration, environment-specific errors, "
        "or one-off chatter. Each {title, body, topic?} is third-person, self-contained.\n"
        "correction_detected/correction: TRUE only when the USER concretely contradicted "
        "or corrected an assistant behavior ('that's wrong, do it like X'); "
        "correction={behavior, do_instead}. Default false.\n"
        "If nothing durable: extracted_knowledge=[], correction_detected=false."
    )


def build_prompt(digest: str) -> str:
    return (
        "SESSION DIGEST (user/assistant prose + tool names; tool outputs omitted):\n"
        f"{digest}\n\n"
        'Return JSON: {"extracted_knowledge": [{"title": <str>, "body": <str>, '
        '"topic": <str|null>}], "correction_detected": <bool>, '
        '"correction": {"behavior": <str>, "do_instead": <str>} | null}'
    )


def parse_ingest(stdout: str, *, max_facts: int = DEFAULT_MAX_FACTS,
                 max_skill_learnings: int = DEFAULT_MAX_SKILL_LEARNINGS,
                 skills_used=None) -> dict:
    """Parse the drain JSON → {facts, correction|None, skill_learnings}.
    GROUNDED-OR-DROPPED: factless entries dropped, facts capped, a correction kept
    only with a non-empty do_instead; a skill_learning kept only when complete AND its
    `skill` is in `skills_used` (the session actually used it — the grounding enforced
    in CODE, not just the prompt). `skills_used=None` → no skill the session can be
    proven to have used → skill_learnings is empty. Malformed JSON → ValueError."""
    data = json.loads(strip_json_fence(stdout))   # JSONDecodeError ⊂ ValueError
    if not isinstance(data, dict):
        raise ValueError("ingest JSON is not an object")
    facts = []
    for f in (data.get("extracted_knowledge") or []):
        if not isinstance(f, dict):
            continue
        title = str(f.get("title", "")).strip()
        body = str(f.get("body", "")).strip()
        if not title or not body:
            continue
        topic = f.get("topic")
        facts.append({"title": title, "body": body,
                      "topic": str(topic).strip() if topic else None})
        if len(facts) >= max_facts:
            break
    corr = None
    c = data.get("correction")
    if data.get("correction_detected") and isinstance(c, dict):
        do = str(c.get("do_instead", "")).strip()
        if do:
            corr = {"behavior": str(c.get("behavior", "")).strip(), "do_instead": do}
    allowed = set(skills_used or ())
    skill_learnings = []
    for s in (data.get("skill_learnings") or []):
        if not isinstance(s, dict):
            continue
        skill = str(s.get("skill", "")).strip()
        title = str(s.get("title", "")).strip()
        body = str(s.get("body", "")).strip()
        if not skill or not title or not body or skill not in allowed:
            continue   # GROUNDED-OR-DROPPED: never a skill the session did not use
        skill_learnings.append({"skill": skill, "title": title, "body": body})
        if len(skill_learnings) >= max_skill_learnings:
            break
    return {"facts": facts, "correction": corr, "skill_learnings": skill_learnings}


def _fact_id(title: str, body: str) -> str:
    return "sing-" + hashlib.sha256(
        f"session-knowledge:{title}:{body}".encode("utf-8")).hexdigest()[:24]


def _correction_id(behavior: str, do_instead: str) -> str:
    return "scorr-" + hashlib.sha256(
        f"session-correction:{behavior}:{do_instead}".encode("utf-8")).hexdigest()[:24]


def _save_facts(conn, facts, *, session_id, ts) -> int:
    """Persist extracted knowledge as recallable memories (node_type=
    'session-knowledge', created_by='background_review' so the loop may later refine
    them). Content-hash id → re-ingest upserts (no duplicates). Redaction is the
    save_memory chokepoint. Per-fact fail-open."""
    n = 0
    for f in facts:
        try:
            memory_lib.save_memory(
                conn, id=_fact_id(f["title"], f["body"]), type="memory",
                title=f["title"][:200], body=f["body"], ts=ts,
                origin_session_id=session_id, node_type="session-knowledge",
                created_by="background_review", topic=f.get("topic"))
            n += 1
        except Exception:
            pass
    return n


def _skill_learning_id(skill: str, title: str, body: str) -> str:
    return "slearn-" + hashlib.sha256(
        f"skill-learning:{skill}:{title}:{body}".encode("utf-8")).hexdigest()[:24]


def _save_skill_learnings(conn, skill_learnings, *, ts) -> int:
    """Persist each grounded skill-learning as the SP-10 / Learnings.md substrate row:
    node_type='learning', index_hook=<skill>, created_by='background_review' — the same
    shape consolidate.py graduates, so these project into the skill's Learnings.md and
    are SP-10-eligible. Content-hash id → idempotent re-ingest upsert. Per-entry
    fail-open (the redaction chokepoint is save_memory)."""
    n = 0
    for s in skill_learnings:
        try:
            memory_lib.save_memory(
                conn, id=_skill_learning_id(s["skill"], s["title"], s["body"]),
                type="learning", title=s["title"][:200], body=s["body"], ts=ts,
                index_hook=s["skill"], node_type="learning",
                created_by="background_review")
            n += 1
        except Exception:
            pass
    return n


def _save_correction(conn, corr, *, session_id, ts) -> int:
    """A detected user correction → a high-signal `feedback` memory (surfaces in the
    SessionStart rehydration gist). The Tier-1 DIRECT skill amendment (north-star §4)
    is the slice-4c follow-on; v1 captures the correction as durable feedback."""
    try:
        behavior = corr.get("behavior", "")
        body = f"Do instead: {corr['do_instead']}"
        if behavior:
            body += f"\n\n(Corrected behavior: {behavior})"
        memory_lib.save_memory(
            conn, id=_correction_id(behavior, corr["do_instead"]), type="feedback",
            title=(behavior or "session correction")[:200], body=body, ts=ts,
            origin_session_id=session_id, node_type="feedback",
            created_by="background_review")
        return 1
    except Exception:
        return 0


def run_session_ingest_pass(conn, *, ts, env, runner=subprocess.run,
                            model=DEFAULT_MODEL, claude_bin="claude",
                            limit=DEFAULT_LIMIT, max_facts=DEFAULT_MAX_FACTS,
                            timeout=DEFAULT_TIMEOUT, max_digest_chars=12000,
                            audit_dir=None, log=lambda _m: None) -> dict:
    """Drain up to `limit` pending sessions: one OAuth `claude` call per session →
    extracted knowledge + correction → the memory store; mark each resolved. GATED
    (SESSION_INGEST_ENABLE; default OFF → a no-op). OAuth-only (run_claude), fail-open
    per session (an error leaves that session un-resolved for retry), idempotent."""
    if not _enabled(env):
        return {"mode": "disabled", "sessions": 0, "ingested": 0, "corrections": 0}
    ingested = corrections = sessions = 0
    for p in pending_sessions(conn, limit=limit):
        try:
            digest = build_digest(p["transcript_path"], max_chars=max_digest_chars)
            if not digest.strip():
                mark_resolved(conn, event_id=p["event_id"])   # nothing to mine
                continue
            stdout = run_claude(build_prompt(digest), model=model, system=build_sys(),
                                claude_bin=claude_bin, timeout=timeout, runner=runner,
                                env=env)
            result = parse_ingest(stdout, max_facts=max_facts)
            ingested += _save_facts(conn, result["facts"],
                                    session_id=p["session_id"], ts=ts)
            if result["correction"]:
                corrections += _save_correction(conn, result["correction"],
                                                session_id=p["session_id"], ts=ts)
            mark_resolved(conn, event_id=p["event_id"])
            sessions += 1
        except Exception as exc:          # per-session fail-open (leave un-resolved)
            log(f"session-ingest failed for {p.get('session_id')}: {exc!r} — retry next run")
    _emit_audit(audit_dir, ts, {"sessions": sessions, "ingested": ingested,
                                "corrections": corrections})
    return {"mode": "ran", "sessions": sessions, "ingested": ingested,
            "corrections": corrections}


def _emit_audit(audit_dir, ts, row) -> None:
    if not audit_dir:
        return
    try:
        path = Path(audit_dir) / f"session-ingest-{str(ts)[:10]}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({**row, "ts": ts}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def beat(conn, config, ts, env):
    """The `run_pipeline` registry entry for the SESSION-INGEST beat (north-star
    subsystem 4): mines each finished session's transcript for durable knowledge +
    user corrections, one OAuth call per session. GATED SESSION_INGEST_ENABLE (the
    drain is a no-op until armed — the ships-active posture flip is the consumer's
    explicit step). Threads the config seam (model + briefings_dir audit)."""
    audit_dir = (Path(config.briefings_dir) / "maintenance-logs"
                 if getattr(config, "briefings_dir", None) else None)
    return run_session_ingest_pass(
        conn, ts=ts, env=env or {}, model=getattr(config, "model", DEFAULT_MODEL),
        audit_dir=audit_dir,
        log=lambda m: sys.stderr.write(f"[session_ingest] {m}\n"))
