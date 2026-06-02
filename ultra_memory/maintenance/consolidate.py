"""The CONSOLIDATE beat — conservative Tier-2 self-improvement drain (project-agnostic).

Ported verbatim-in-logic from Trading's scripts/maintenance/consolidate_candidates.py
(SP-6 §6.6, D8/D9/D10/D13) into the portable engine. The CONSERVATIVE half of the
self-improvement loop: it ONLY ADDS. It drains accumulated
``session_events WHERE kind='skill_learning_candidate' AND resolved=0`` rows
(bounded N) through exactly ONE batched OAuth ``claude`` call and GRADUATES the
durable lessons into the knowledge store. The AGGRESSIVE rewrite / revert /
quarantine of existing units is the separate aggressive beat (its own risk wall).

Flow:
  1. READ un-resolved candidates (bounded to N; default 50).
  2. DEDUP pre-filter (NO LLM): ``unified_recall`` (cosine/BM25) per candidate.
  3. ONE batched prompt → ``ultra_memory.claude_cli.run_claude`` ONCE (OAuth;
     INJECTABLE runner so tests never spawn a process).
  4. PARSE a per-candidate plan: ``graduate`` | ``merge`` | ``skip-transient``.
  5. APPLY deterministically + provenance-gated:
       graduate → ``save_memory(created_by='background_review')`` + a
                  ``record_link('validated_as')`` edge (memory-only — no auto wiki page);
       merge    → consumer wiki ``append-validation-log`` into an existing page;
       skip     → no write.
     The apply path REFUSES any action targeting a ``created_by='human'`` or
     ``pinned`` unit (D10 — in code, not just the prompt).
  6. MARK each handled candidate ``resolved=1``; emit an audit row + a human line.

HARD INVARIANTS (preserved from the Trading original):
  * OAuth-only — every LLM call via ``ultra_memory.claude_cli.run_claude``; NEVER the
    metered SDK / API-key path.
  * Exactly ONE LLM call per run.
  * CONSERVATIVE-only (D10): adds graduated knowledge; never rewrites/reverts/deletes
    an existing unit; provenance gate refuses ``human``/``pinned``.
  * Bounded blast radius: a per-run graduation cap + the bounded read N.
  * Fail-open: a runner error / unparseable plan degrades to a no-op + one diagnostic
    line; candidates left UN-resolved for the next run; NEVER raises into the caller.

PROJECT-AGNOSTIC seam (the only consumer couplings, all routed through config):
  * the model — ``MaintenanceConfig.model`` (the registry adapter passes it);
  * the audit dir — ``MaintenanceConfig.briefings_dir`` (None → no audit file, just a
    stderr summary line);
  * the wiki write — ``MaintenanceConfig.wiki_gateway`` (the consumer's wiki_lib CLI;
    None → no wiki, so a ``merge`` decision degrades to a logged skip instead of
    leaving the candidate to re-drain forever).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ultra_memory import memory_lib
from ultra_memory._time import now_utc_zulu
from ultra_memory.claude_cli import run_claude          # the OAuth chokepoint
from ultra_memory.unified_query import unified_recall


# Default bounded read N (D8 — calibrate fan-out to stakes; one batched call).
DEFAULT_LIMIT = 50
# Default per-run graduation cap (bounded blast radius — D10).
DEFAULT_MAX_GRADUATIONS = 20
# Internal bundled-call timeout (seconds) — kept under the outer shell gtimeout.
DEFAULT_TIMEOUT = 720
# Project-agnostic: no hardcoded model / audit path. The registry adapter supplies
# both from the resolved MaintenanceConfig; a direct caller passes them explicitly.
DEFAULT_MODEL = "claude-sonnet-4-6"
# Module-level audit-dir default. None → the drain writes NO audit file (just the
# stderr summary line). A consumer points this at <briefings>/maintenance-logs via
# the registry adapter; tests monkeypatch it or pass audit_dir explicitly.
AUDIT_DIR: Path | None = None


def _warn(msg: str) -> None:
    print(f"[consolidate] {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# 1. READ un-resolved candidates (bounded)
# --------------------------------------------------------------------------- #

def _skill_of(title: str) -> str:
    """Derive the skill_tag from the candidate title. The Stop hook formats it as
    ``"{skill}: skill invoked, ..."`` — the prefix before the first ``: `` is the
    skill. Falls back to the whole title if there is no delimiter."""
    head = (title or "").split(":", 1)[0].strip()
    return head or "unknown-skill"


def read_candidates(conn, *, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Return up to `limit` un-resolved skill_learning_candidate rows (oldest-first),
    each a dict {id, session_id, title, detail, outcome_signal, skill, resolved}."""
    rows = conn.execute(
        "SELECT id, session_id, ts, title, detail, outcome_signal, resolved "
        "FROM session_events WHERE kind='skill_learning_candidate' AND resolved=0 "
        "ORDER BY id LIMIT ?",
        (int(limit),),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["skill"] = _skill_of(d.get("title", ""))
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# 2. DEDUP pre-filter (NO LLM) — unified_recall cosine/BM25
# --------------------------------------------------------------------------- #

def dedup_prefilter(conn, candidates: list[dict], *, embedder, top_k: int = 3) -> list[dict]:
    """Attach the top existing-store matches to each candidate for the LLM's dedup
    context. NO LLM — ``unified_recall`` is cosine/BM25 only. The orchestrator path
    (``agent_topics=None``) sees the full store; failures degrade to an empty
    context (fail-open — the LLM still gets the candidate)."""
    enriched = []
    for c in candidates:
        query = c.get("title") or ""
        matches: list[dict] = []
        try:
            hits = unified_recall(
                conn, query, caller_class="orchestrator", agent_topics=None,
                embedder=embedder, top_k=top_k, audit=False)
            for h in hits:
                matches.append({
                    "source_kind": h.get("source_kind"),
                    "id": h.get("id") or h.get("slug"),
                    "title": h.get("title"),
                    "snippet": (h.get("snippet") or h.get("body") or "")[:240],
                    "path": h.get("path"),
                })
        except Exception as exc:  # fail-open: no context, never wedge
            _warn(f"dedup pre-filter recall failed for candidate {c.get('id')}: {exc}")
        e = dict(c)
        e["dedup_context"] = matches
        enriched.append(e)
    return enriched


# --------------------------------------------------------------------------- #
# 3. ONE batched prompt
# --------------------------------------------------------------------------- #

def build_sys() -> str:
    """The CONSERVATIVE consolidation system prompt (D9/D10).

    States the conservative boundary IN THE PROMPT (the gate is ALSO enforced in
    code): never rewrite / revert / delete an existing unit; only ADD graduated
    knowledge or MERGE into an existing page; never touch human/pinned units."""
    return (
        "You are the conservative self-improvement consolidation reviewer. You "
        "receive a JSON list of skill-learning CANDIDATES (each captured because a "
        "skill ran without its Learnings.md being updated) plus, per candidate, the "
        "top existing store matches (dedup context). For EACH candidate decide ONE "
        "action and return ONLY a JSON object {\"decisions\": [...]} — no prose, no "
        "markdown fences. Each decision is one of:\n"
        '  - "graduate": {"candidate_id":<id>,"action":"graduate","skill":<skill_tag>,'
        '"title":<short title>,"body":<the durable lesson>,"reason":<str>} — promote a '
        "NEW durable lesson into the store as a memory row tagged to the skill. "
        "(This beat graduates skill-lessons as memory rows only; auto-creating NEW wiki "
        "pages from a candidate is out of scope — use \"merge\" to reinforce an "
        "existing page, or leave a domain-durable lesson as a graduated memory.)\n"
        '  - "merge": {"candidate_id":<id>,"action":"merge","page":<existing wiki page '
        'path>,"entry":<one validation-log line>,"reason":<str>} — the lesson reinforces '
        "an EXISTING page; append a validation-log entry (NEVER rewrite the page).\n"
        '  - "skip-transient": {"candidate_id":<id>,"action":"skip-transient",'
        '"reason":<str>} — the candidate is not durable.\n'
        "CONSERVATIVE BOUNDARY (hard): you may ONLY add new graduated knowledge or "
        "merge a validation-log entry. You may NEVER rewrite, revert, archive, or "
        "delete an existing learning/page, and NEVER target a human-authored or "
        "pinned unit. If a candidate duplicates an existing unit, prefer merge or "
        "skip-transient over a new duplicate.\n"
        "ANTI-CAPTURE GUARDRAILS: NEVER graduate a TRANSIENT or ENVIRONMENT-dependent "
        "observation (a one-off flake, a path/permission/network hiccup, routine "
        "skill-invocation noise, anything tied to a single session's environment). "
        "Graduate ONLY a lesson that is durable, recurring, or carries a positive "
        "outcome signal, and is NOT already covered by the dedup context."
    )


def build_prompt(enriched: list[dict]) -> str:
    """The batched user prompt: the candidates + their dedup context + a restated
    anti-capture reminder so the guardrails survive even a system-prompt override."""
    payload = {"candidates": [
        {"candidate_id": c["id"], "skill": c.get("skill"),
         "title": c.get("title"), "detail": c.get("detail"),
         "outcome_signal": c.get("outcome_signal"),
         "dedup_context": c.get("dedup_context", [])}
        for c in enriched
    ]}
    return (
        "Consolidate the following skill-learning candidates. For each, decide "
        "graduate | merge | skip-transient per your system instructions. Apply the "
        "anti-capture guardrails: never persist a transient or environment-dependent "
        "observation; graduate only durable/recurring lessons not already in the "
        'dedup context. Return ONLY {"decisions": [...]}.\n\n'
        + json.dumps(payload, indent=2, ensure_ascii=False)
    )


def _parse_plan(stdout: str) -> list[dict]:
    """Parse the bundled LLM response into a list of decision dicts. Robust to a
    leading ```json fence. Raises ValueError on any deviation so the caller fails
    closed (no-op, candidates left un-resolved)."""
    text = (stdout or "").strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    data = json.loads(text)  # JSONDecodeError is a ValueError subclass
    if not isinstance(data, dict) or "decisions" not in data:
        raise ValueError("response JSON missing top-level 'decisions' list")
    decisions = data["decisions"]
    if not isinstance(decisions, list):
        raise ValueError("'decisions' is not a list")
    return decisions


# --------------------------------------------------------------------------- #
# 4./5. APPLY — provenance-gated, deterministic
# --------------------------------------------------------------------------- #

def _is_protected(conn, mem_id: str) -> bool:
    """The PROVENANCE GATE (D10): a memory is protected (un-editable by the
    consolidation) if it is created_by='human' OR pinned. Reads the live row; a
    missing id is NOT protected (a new graduation target). Fail-closed: any read
    error treats the unit as PROTECTED (refuse rather than risk an edit)."""
    try:
        row = conn.execute(
            "SELECT created_by, pinned FROM memories WHERE id=?", (mem_id,)
        ).fetchone()
    except Exception:
        return True
    if row is None:
        return False
    return str(row["created_by"]) == "human" or bool(row["pinned"])


def _graduation_id(decision: dict) -> str:
    """A deterministic id for a graduated memory (idempotent on the lesson body)."""
    skill = decision.get("skill") or "unknown-skill"
    body = decision.get("body") or decision.get("title") or ""
    raw = f"graduated:{skill}:{body}"
    return "grad-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _mark_resolved(conn, candidate_id) -> None:
    """Route the resolve through the engine's bounded busy-retry txn
    (`memory_lib._write_txn`: BEGIN IMMEDIATE / COMMIT with retry-with-backoff on
    SQLITE_BUSY + durable spool on exhaustion), so a (rare) lock-contention raise
    after a committed wiki write can't leave the candidate un-resolved. work() is
    re-runnable (a plain idempotent UPDATE)."""
    def work():
        conn.execute(
            "UPDATE session_events SET resolved=1 WHERE id=?", (candidate_id,))

    memory_lib._write_txn(conn, work, spool={
        "op": "consolidate_mark_resolved", "candidate_id": candidate_id})


def _default_apply_merge(*, page, entry, project_dir, runner, topic, wiki_gateway) -> bool:
    """Shell ``uv run <wiki_gateway> append-validation-log`` through the injected
    runner (so tests never spawn a process). NEVER rewrites the page — the verb only
    appends a validation-log entry.

    Returns True iff the wiki write SUCCEEDED (rc==0). On a non-zero exit (e.g. the
    target page doesn't exist → wiki_lib raises ValueError → rc=1) it returns False
    so the caller leaves the source candidate UN-resolved — never lose a learning,
    mirroring the graduate branch's raise→un-resolved semantics."""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as tf:
        tf.write(entry)
        tmp = tf.name
    try:
        proc = runner(
            ["uv", "run", str(wiki_gateway), "append-validation-log",
             "--page", str(page), "--from-file", tmp, "--topic", topic],
            capture_output=True, text=True, cwd=str(project_dir),
        )
        if getattr(proc, "returncode", 0) != 0:
            _warn(f"append-validation-log failed for {page}: "
                  f"{(getattr(proc, 'stderr', '') or '')[:300]}")
            return False
        return True
    finally:
        Path(tmp).unlink(missing_ok=True)


def _topic_from_page(page: str, default_topic: str = "default") -> str:
    """Derive the --topic from a wiki-relative page path (wiki/<topic>/...);
    defaults to *default_topic* when the path doesn't carry a topic segment."""
    parts = Path(page).parts
    if len(parts) >= 3 and parts[0] == "wiki":
        return parts[1]
    return default_topic


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def consolidate(conn, *, runner=subprocess.run, embedder, env=None,
                project_dir=None, limit=DEFAULT_LIMIT,
                max_graduations=DEFAULT_MAX_GRADUATIONS, model=None,
                timeout=DEFAULT_TIMEOUT, claude_bin="claude",
                apply_merge=None, audit_dir=None, wiki_gateway=None,
                default_topic="default", ts=None) -> dict:
    """Run ONE conservative consolidation drain. Returns a summary dict; NEVER
    raises (fail-open). `apply_merge` is injectable so tests don't shell wiki_lib;
    `audit_dir` overrides the audit-log dir (None → no audit file); `wiki_gateway`
    is the consumer's wiki CLI (None → a merge decision degrades to a logged skip)."""
    if model is None:
        model = DEFAULT_MODEL
    if apply_merge is None:
        apply_merge = _default_apply_merge
    if audit_dir is None:
        audit_dir = AUDIT_DIR
    if ts is None:
        ts = now_utc_zulu()
    project_dir = Path(project_dir) if project_dir is not None else Path.cwd()

    summary = {
        "op": "consolidate", "ts": ts, "candidates": 0,
        "graduated": 0, "merged": 0, "skipped": 0, "refused": 0,
        "merge_failed": 0,   # merges whose wiki write failed (left un-resolved)
        "cap_hit": False, "error": False, "per_skill": {},
    }

    candidates = read_candidates(conn, limit=limit)
    summary["candidates"] = len(candidates)

    # skip-if-empty — ZERO LLM calls.
    if not candidates:
        _emit_audit(summary, audit_dir)
        return summary

    by_id = {c["id"]: c for c in candidates}

    try:
        enriched = dedup_prefilter(conn, candidates, embedder=embedder)
        prompt = build_prompt(enriched)
        sys_prompt = build_sys()
        stdout = run_claude(prompt, model=model, system=sys_prompt,
                            claude_bin=claude_bin, timeout=timeout,
                            runner=runner, env=env)
        decisions = _parse_plan(stdout)
    except Exception as exc:
        # Fail-open: one diagnostic line; candidates left UN-resolved for next run.
        _warn(f"consolidation drain failed ({exc}) — no-op; candidates left "
              f"un-resolved for the next run.")
        summary["error"] = True
        _emit_audit(summary, audit_dir)
        return summary

    for decision in decisions:
        try:
            _apply_one(conn, decision, by_id=by_id, summary=summary,
                       max_graduations=max_graduations, project_dir=project_dir,
                       runner=runner, apply_merge=apply_merge,
                       wiki_gateway=wiki_gateway, default_topic=default_topic, ts=ts)
        except Exception as exc:  # per-decision fail-open
            _warn(f"apply failed for decision {decision.get('candidate_id')}: {exc}")

    _emit_audit(summary, audit_dir)
    return summary


def _apply_one(conn, decision, *, by_id, summary, max_graduations, project_dir,
               runner, apply_merge, wiki_gateway, default_topic, ts) -> None:
    """Apply ONE decision, provenance-gated. Marks the candidate resolved only when
    the action is fully applied (a capped/refused graduation leaves it un-resolved
    for the next run)."""
    cand_id = decision.get("candidate_id")
    action = decision.get("action")
    cand = by_id.get(cand_id)
    if cand is None:
        _warn(f"decision references unknown candidate_id {cand_id!r}; skipping")
        return
    skill = decision.get("skill") or cand.get("skill") or "unknown-skill"

    # PROVENANCE GATE (D10): any decision that names a target_id which is an
    # existing human/pinned unit is REFUSED — the consolidation never edits a
    # protected unit. (The graduate/merge verbs only ADD, but a malformed plan
    # could try to re-target an existing id; the gate is the in-code fence.)
    target_id = decision.get("target_id")
    if target_id and _is_protected(conn, target_id):
        _warn(f"REFUSED: decision targets protected (human/pinned) unit "
              f"{target_id!r} — provenance gate (D10).")
        summary["refused"] += 1
        return

    if action == "skip-transient":
        _mark_resolved(conn, cand_id)
        summary["skipped"] += 1
        return

    if action == "merge":
        page = decision.get("page")
        entry = decision.get("entry")
        if not page or not entry:
            _warn(f"merge decision for {cand_id} missing page/entry; skipping")
            return
        # PROJECT-AGNOSTIC degrade: no wiki configured AND we'd rely on the built-in
        # wiki writer → there is no page to merge into. Resolve the candidate as a
        # skip (don't re-drain it forever) and log it. A caller that injects a custom
        # `apply_merge` takes responsibility for the write, so the degrade is skipped.
        if wiki_gateway is None and apply_merge is _default_apply_merge:
            _warn(f"merge decision for {cand_id} but no wiki_gateway configured — "
                  f"resolving as skip (no wiki to merge into)")
            _mark_resolved(conn, cand_id)
            summary["skipped"] += 1
            return
        ok = apply_merge(page=page, entry=entry, project_dir=project_dir, runner=runner,
                         topic=_topic_from_page(page, default_topic), wiki_gateway=wiki_gateway)
        # Only mark resolved + count when the wiki write SUCCEEDED. A False return
        # (the merge write failed) leaves the candidate resolved=0 so the next drain
        # re-selects it — never lose a learning. An injected apply_merge that returns
        # None (test/no-op writers) is treated as success (back-compat); only an
        # explicit False is a failure.
        if ok is False:
            summary["merge_failed"] = summary.get("merge_failed", 0) + 1
            _warn(f"merge for candidate {cand_id} failed — left un-resolved "
                  f"(page={page})")
            return
        _mark_resolved(conn, cand_id)
        summary["merged"] += 1
        summary["per_skill"][skill] = summary["per_skill"].get(skill, 0) + 1
        return

    if action == "graduate":
        # Bounded blast radius (D10): stop graduating once the per-run cap is hit;
        # leave the rest un-resolved for the next run.
        if summary["graduated"] >= max_graduations:
            summary["cap_hit"] = True
            return
        body = decision.get("body")
        title = decision.get("title")
        if not body or not title:
            _warn(f"graduate decision for {cand_id} missing title/body; skipping")
            return
        grad_id = _graduation_id(decision)
        # Defensive provenance gate (D10, belt-and-suspenders): the grad-<sha> id
        # space is disjoint from any human/pinned id by construction, so this should
        # never fire — but enforce the gate on the PRIMARY graduate write too (not
        # only the re-target vector), fail-closed, so NO path that could touch an
        # existing row bypasses it.
        if _is_protected(conn, grad_id):
            _warn(f"graduate target {grad_id} is protected (human/pinned) — refusing (provenance gate)")
            return
        memory_lib.save_memory(
            conn, id=grad_id, type="memory", title=title, body=body, ts=ts,
            index_hook=skill, node_type="learning", created_by="background_review")
        # validated_as link from the SOURCE event to the graduated memory.
        memory_lib.record_link(
            conn, src_kind="session_event", src_id=str(cand_id),
            predicate="validated_as", dst_kind="memory", dst_id=grad_id, ts=ts)
        _mark_resolved(conn, cand_id)
        summary["graduated"] += 1
        summary["per_skill"][skill] = summary["per_skill"].get(skill, 0) + 1
        return

    _warn(f"unknown action {action!r} for candidate {cand_id}; skipping")


def _emit_audit(summary: dict, audit_dir) -> None:
    """Append one JSON audit row + print a human summary line. Fail-open. When
    *audit_dir* is None (project-agnostic default), only the stderr line is emitted."""
    if audit_dir is not None:
        try:
            audit_dir = Path(audit_dir)
            target = audit_dir / f"consolidation-{datetime.now(timezone.utc).date().isoformat()}.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(summary, ensure_ascii=False) + "\n")
        except Exception as exc:  # pragma: no cover — never let audit wedge the drain
            _warn(f"audit write failed: {exc}")
    cap = " (graduation cap HIT)" if summary.get("cap_hit") else ""
    err = " [ERROR — fail-open no-op]" if summary.get("error") else ""
    mf = f", {summary['merge_failed']} merge-failed" if summary.get("merge_failed") else ""
    print(
        f"consolidation: {summary['candidates']} candidate(s) → "
        f"{summary['graduated']} graduated, {summary['merged']} merged, "
        f"{summary['skipped']} skipped, {summary['refused']} refused{mf}{cap}{err}",
        file=sys.stderr,
    )


def _resolve_embedder():
    """The engine's fastembed-backed default embedder, or None (BM25-only dedup).
    fastembed is the OPTIONAL 'retrieval' extra; the engine fail-opens to BM25 when
    there is no embedder, so degrade gracefully rather than crash."""
    from ultra_memory import retrieval_core
    try:
        return retrieval_core.default_embedder()
    except Exception as exc:  # fastembed absent → BM25-only dedup
        sys.stderr.write(f"note: consolidate: no embedder ({exc}); BM25-only dedup (fail-open)\n")
        return None


# --------------------------------------------------------------------------- #
# Registry adapter — the beat signature the pipeline orchestrator calls.
# --------------------------------------------------------------------------- #

def beat(conn, config, ts, env):
    """The `run_pipeline` registry entry. Threads the resolved MaintenanceConfig
    seam (model / audit dir / wiki gateway / default topic) into `consolidate`."""
    audit_dir = (config.briefings_dir / "maintenance-logs") if config.briefings_dir else None
    default_topic = config.topics[0] if getattr(config, "topics", None) else "default"
    return consolidate(
        conn, embedder=_resolve_embedder(), env=env,
        project_dir=config.project_dir, model=config.model,
        audit_dir=audit_dir, wiki_gateway=config.wiki_gateway,
        default_topic=default_topic, ts=ts)


# --------------------------------------------------------------------------- #
# CLI — `python -m ultra_memory.maintenance.consolidate --db <path>`. The package
# CLI (`python -m ultra_memory.maintenance`) is the throttled pipeline entry; this
# is a single-beat debug/manual entry. The live drain is orchestrator-driven.
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Conservative Tier-2 self-improvement consolidation drain.")
    ap.add_argument("--db", required=True, help="path to memory.db")
    ap.add_argument("--project-dir", default=str(Path.cwd()))
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"max candidates to drain per run (default {DEFAULT_LIMIT})")
    ap.add_argument("--max-graduations", type=int, default=DEFAULT_MAX_GRADUATIONS,
                    help=f"per-run graduation cap (default {DEFAULT_MAX_GRADUATIONS})")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--wiki-gateway", default=None,
                    help="consumer wiki_lib CLI for the merge verb (omit → no wiki)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = ap.parse_args(argv)

    conn = memory_lib.open_memory_db(args.db)
    try:
        summary = consolidate(
            conn, embedder=_resolve_embedder(), project_dir=Path(args.project_dir),
            limit=args.limit, max_graduations=args.max_graduations,
            model=args.model, timeout=args.timeout,
            wiki_gateway=Path(args.wiki_gateway) if args.wiki_gateway else None)
    finally:
        conn.close()
    return 1 if summary.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
