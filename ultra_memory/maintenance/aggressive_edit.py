"""SP-7 §5.1 — the GEPA-lite TRACE-REFLECTIVE AUTO-EDIT track — Stage 5 of the
SP-7 build (spec §7 step 5). The FIRST of the three aggressive self-improvement
capabilities, built ON TOP of the safety wall (Stages 1-4): it does not re-implement
any guard — it COMPOSES the wall (`aggressive_wall`), the eval gate
(`aggressive_eval`), the bounds (`aggressive_bounds`), and the outcome signal
(`aggressive_outcomes`).

THE GEPA-LITE LOOP (spec §5.1):
  0. SELECT candidates with NO LLM — an agent-authored unit with >= MIN_EVIDENCE
     linked outcomes whose `outcome_weight` is mixed / trending-down (the
     "sharpen it" zone — below a threshold but NOT a hard regression; a hard
     regression belongs to the §5.2 self-reversion track, not here), OR a
     conservative-pass near-dup-not-merge pair (the "two lessons should be one
     sharper lesson" case — supplied by the caller).
  1. REFLECT via ONE batched OAuth call (an INJECTED runner — never a direct
     `claude` spawn here): feed each candidate unit + its TRACE (the linked
     session_events + their outcome_signals + the dedup context) and ask for a
     TARGETED diff (reword / sharpen / merge-two-into-one / correct a factual
     error) — NOT a yes/no, NOT a free rewrite. Each diff MUST cite which trace
     evidence motivates it; an UNGROUNDED "improvement" is dropped at plan-parse
     (the eval-reject the spec calls for — "an un-grounded improvement is
     rejected").
  2. EVAL GATE (delegated to `aggressive_eval.run_aggressive_eval`): the hard gate
     (zero-tolerance provenance, halt-the-run) + the deterministic shadow quality
     gate (reject a probe-regressing edit). Only admitted edits proceed.
  3. APPLY (only through the wall + bounded): each admitted edit goes through
     `aggressive_wall.apply_auto_edit` — a redirect-preserving new version
     (save_memory(created_by='background_review') + consolidate so the OLD becomes
     a recoverable `redirect`) + a `superseded_by` link carrying the trace refs as
     evidence. Bounded to MAX_EDITS_PER_RUN, HALT-ON-EXCEED (§4c). assert_mutable
     RE-READS the live row at apply time (the wall's job) — a single forbidden
     target halts the whole apply (§4a stop-the-world, zero tolerance, NOT a skip).

THE WALL LIVES IN THE APPLY PATH (code), NEVER ONLY THE PROMPT (spec §4 design
rule + the [[feedback-subagents-can-leak-secrets]] lesson: build the constraint
into the TOOL). The LLM *proposes* the diff; the eval gate + the wall + the bound
*enforce*. The prompt asks for a grounded targeted diff, but a model that ignores
that is caught downstream: an ungrounded diff is dropped at parse, a degrading
diff is rejected at eval, a forbidden target halts at the wall, an over-cap batch
halts at the bound.

OAUTH-ONLY (HARD): the ONE reflection call routes through an INJECTED runner that
mimics `ultra_memory.claude_cli.run_claude`'s contract (the precedent:
score_news.py's injectable runner, judge_borderline's `runner=`). Tests inject a
fake runner and NEVER spawn `claude`; there is NO anthropic-SDK import, no
API-key env read, no direct messages API, no prompt-cache control anywhere (a
guard test asserts it). The default runner (only ever used in a real run, never in
a test) is the OAuth `claude` CLI subprocess, imported lazily so the module imports
clean with no CLI present.

ARCHIVE-NEVER-DELETE: every verb is a reversible FSM transition / redirect-stub via
the wall primitives — NO rm / os.remove / memory_lib.delete anywhere (a guard test
asserts it). FAIL-OPEN: any error in reflection / parse / eval degrades to an EMPTY
plan or a no-op apply — it never raises out into the nightly / monthly maintenance
run (the one exception is a ForbiddenTargetError from the wall, which is the §4a
stop-the-world the eval hard-gate is supposed to have caught first; in the
apply path it propagates as the zero-tolerance halt).

The engine primitives the wall consumes (save_memory / consolidate / record_link /
set_status) are GENERIC + already on live master (ffcd414). The selection trigger,
the reflection prompt, the grounding rule, and the MAX_EDITS policy are the
consumer's policy (e.g. a trading project).
"""
from __future__ import annotations

import sys
from pathlib import Path

# The sibling SP-7 modules — the wall (apply path), the eval gate, the bounds, the
# outcome signal. This track COMPOSES them; it re-implements no guard.
from ultra_memory.maintenance import aggressive_outcomes as ao  # noqa: E402
from ultra_memory.maintenance.aggressive_bounds import MAX_EDITS_PER_RUN  # noqa: E402
from ultra_memory.maintenance.aggressive_eval import run_aggressive_eval  # noqa: E402
from ultra_memory.maintenance.aggressive_wall import (  # noqa: E402
    MemoryUnit,
    apply_auto_edit,
    assert_mutable,
)

# Shared OAuth-call + JSON-extract plumbing (the OAuth chokepoint lives there).
from ultra_memory.maintenance.aggressive_utils import (  # noqa: E402
    call_model,
    default_runner,
    extract_json,
)

# --------------------------------------------------------------------------- #
# §5.1 selection policy — the no-LLM "sharpen it" trigger band.
# --------------------------------------------------------------------------- #

# The OAuth model the ONE reflection call uses (Sonnet-tier, like judge_borderline).
REFLECT_MODEL = "claude-sonnet-4-6"

# The edit-trigger weight threshold: a unit is a "sharpen it" candidate when its
# outcome_weight is BELOW this (its outcomes are mixed / trending-down) but it is
# NOT a hard regression (net-negative + below baseline — that is the §5.2 reversion
# track's domain, not the edit track's). 1.0 is the inert default; a sub-1.0 weight
# means the recency-weighted outcomes lean negative — the mediocre lesson to sharpen.
EDIT_WEIGHT_THRESHOLD = 1.0

# Per-run cap (mirrors the bound the apply enforces). Default from aggressive_bounds.
MAX_EDITS = MAX_EDITS_PER_RUN

# How many of a unit's most-recent outcome events to surface in the trace prompt
# (the GEPA-lite "trace" — bounded so the batched prompt stays cheap).
TRACE_OUTCOMES_CAP = 20


# --------------------------------------------------------------------------- #
# 0. No-LLM candidate selection (the §5.1 trigger).
# --------------------------------------------------------------------------- #

def _agent_authored_active(conn) -> list[dict]:
    """Every unit the loop may even reason over: agent-authored, active, unpinned.
    (The provenance WALL gates the WRITE; we ALSO restrict SELECT so the loop never
    reasons over human/pinned rows.) Fail-closed-to-empty on a read error."""
    try:
        rows = conn.execute(
            "SELECT id, outcome_weight FROM memories "
            "WHERE created_by IN ('agent','background_review') "
            "AND status='active' AND pinned=0"
        ).fetchall()
    except Exception:
        return []
    return [{"id": r["id"], "outcome_weight": r["outcome_weight"]} for r in rows]


def select_edit_candidates(conn, *, near_dup_pairs=None) -> list[dict]:
    """The no-LLM §5.1 trigger. Returns the candidate units for the edit track:

      (a) an agent-authored unit with >= MIN_EVIDENCE linked outcomes whose
          outcome_weight is mixed / trending-down (< EDIT_WEIGHT_THRESHOLD) but is
          NOT a hard regression (handled by the §5.2 reversion track), PLUS
      (b) the OLD member of any conservative-pass near-dup-not-merge pair the
          caller supplies (`near_dup_pairs`: a list of {old_id, dup_id} — the
          "two lessons should be one sharper lesson" case).

    Each result is {unit_id, trigger, outcome_weight, n_outcomes}. NEVER picks a
    human / pinned / sub-evidence / healthy-high-weight unit. Fail-open-to-empty
    on any error (one bad row never aborts the scan)."""
    out: list[dict] = []
    seen: set = set()
    for unit in _agent_authored_active(conn):
        uid = unit["id"]
        try:
            n = len(ao.linked_outcomes(conn, uid))
            if n < ao.MIN_EVIDENCE:
                continue                      # too little evidence (conservative floor)
            weight = unit["outcome_weight"]
            if weight is None or weight >= EDIT_WEIGHT_THRESHOLD:
                continue                      # healthy / inert → leave it alone
            # A HARD regression is the reversion track's domain, not the edit track.
            if ao.is_regression(conn, regressed_id=uid, prior_id=None):
                continue
            out.append({"unit_id": uid, "trigger": "mixed_trending_down",
                        "outcome_weight": weight, "n_outcomes": n})
            seen.add(uid)
        except Exception:
            continue                          # fail-open per unit

    # (b) caller-supplied near-dup-not-merge pairs (the conservative pass surfaces
    # these; we sharpen the OLD member into the merged lesson). The dup_id rides
    # along as merge context for the reflection.
    for pair in (near_dup_pairs or []):
        try:
            old_id = pair.get("old_id")
            if not old_id or old_id in seen:
                continue
            # Respect the provenance wall at select-time too (belt-and-suspenders).
            assert_mutable(conn, MemoryUnit(old_id))
            out.append({"unit_id": old_id, "trigger": "near_dup_not_merge",
                        "dup_id": pair.get("dup_id"), "outcome_weight": None,
                        "n_outcomes": None})
            seen.add(old_id)
        except Exception:
            continue                          # a forbidden / bad pair is skipped here
    return out


# --------------------------------------------------------------------------- #
# 1a. The trace bundle (the GEPA-lite substrate).
# --------------------------------------------------------------------------- #

def build_trace(conn, unit_id, *, dup_id=None) -> dict:
    """Bundle a candidate's reflection substrate: the unit body/title + its linked
    outcome events (each {event_id, ts, outcome_signal}) + the optional dedup
    context (a near-dup unit's body, when the trigger is near_dup_not_merge).

    The `event_id`s are the trace refs a grounded diff must cite (the §5.1
    "cite which trace evidence motivates it"). Fail-soft: a missing unit yields a
    minimal trace (the reflection then has nothing to ground on → its diff is
    dropped as ungrounded downstream — the safe outcome)."""
    row = conn.execute(
        "SELECT title, body, type FROM memories WHERE id=?", (unit_id,)).fetchone()
    title = row["title"] if row else ""
    body = row["body"] if row else ""

    outcomes: list[dict] = []
    try:
        rows = conn.execute(
            """
            SELECT se.id AS event_id, se.ts AS ts,
                   se.outcome_signal AS outcome_signal, se.title AS title
              FROM links l
              JOIN session_events se ON se.id = CAST(l.src_id AS INTEGER)
             WHERE l.dst_kind = 'memory' AND l.dst_id = ?
               AND l.src_kind = 'session_event'
               AND l.predicate IN ('validated_as','superseded_by')
               AND se.outcome_signal IS NOT NULL
             ORDER BY se.ts DESC, se.id DESC
             LIMIT ?
            """,
            (unit_id, TRACE_OUTCOMES_CAP),
        ).fetchall()
        outcomes = [
            {"event_id": f"ev-{r['event_id']}", "ts": r["ts"],
             "outcome_signal": r["outcome_signal"], "title": r["title"]}
            for r in rows
        ]
    except Exception:
        outcomes = []

    dup_body = None
    if dup_id:
        drow = conn.execute(
            "SELECT body FROM memories WHERE id=?", (dup_id,)).fetchone()
        dup_body = drow["body"] if drow else None

    return {"unit_id": unit_id, "title": title, "body": body,
            "outcomes": outcomes, "dup_id": dup_id, "dup_body": dup_body}


# --------------------------------------------------------------------------- #
# 1b. The reflection prompt — a TARGETED diff, grounded, NOT a free rewrite.
# --------------------------------------------------------------------------- #

_REFLECT_SYSTEM = (
    "You are the maintenance reflection step of an autonomous knowledge-curation "
    "loop. You propose TARGETED, evidence-grounded improvements to mediocre "
    "agent-authored learnings — you NEVER rewrite freely and you NEVER invent."
)


def build_reflection_prompt(traces: list[dict]) -> str:
    """Build the ONE batched reflection prompt over all candidate traces (spec
    §5.5: one batched call). The prompt CONSTRAINS the model to a TARGETED diff
    (reword / sharpen / merge-two / correct), each diff MUST cite the trace
    evidence (the `event_id`s) that motivates it, and the reply MUST be a single
    JSON object {\"edits\": [...]} — nothing else.

    The grounding constraint is encoded in the PROMPT here, AND enforced in CODE at
    plan-parse (`_parse_plan` drops an ungrounded diff) — defense-in-depth, never
    trusting the prompt alone."""
    lines: list[str] = [
        "TASK: for each agent-authored learning below, propose AT MOST ONE "
        "TARGETED diff — a reword, a sharpening, a merge-two-into-one, or a "
        "factual correction — grounded in the unit's TRACE evidence. This is NOT a "
        "free rewrite: change only what the trace evidence motivates.",
        "",
        "HARD RULES:",
        "  * Every proposed edit MUST cite the trace `evidence` (one or more "
        "event_id from the unit's outcomes) that motivates it. An edit with no "
        "evidence citation will be DISCARDED.",
        "  * If a unit needs no change, propose no edit for it (omit it).",
        "  * Reply with a SINGLE JSON object and nothing else: "
        '{"edits": [{"old_id": ..., "new_title": ..., "new_body": ..., '
        '"evidence": "ev-123,ev-456"}, ...]}.',
        "",
        "UNITS + TRACES:",
    ]
    for t in traces:
        lines.append(f"--- unit_id: {t['unit_id']} ---")
        lines.append(f"title: {t.get('title','')}")
        lines.append(f"body: {t.get('body','')}")
        outs = t.get("outcomes", [])
        if outs:
            lines.append("trace_outcomes (most recent first):")
            for o in outs:
                lines.append(
                    f"  - {o['event_id']} [{o.get('outcome_signal')}] "
                    f"{o.get('title','')}")
        if t.get("dup_body"):
            lines.append(
                f"near_duplicate (consider MERGING into one sharper lesson) "
                f"[{t.get('dup_id')}]: {t['dup_body']}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 1c. Reflect — ONE batched call through the INJECTED runner, parse, ground-check.
# --------------------------------------------------------------------------- #


def _is_grounded(edit: dict) -> bool:
    """An edit is GROUNDED iff it cites non-empty trace evidence (spec §5.1). An
    ungrounded "improvement" is dropped at parse (the eval-reject)."""
    ev = edit.get("evidence")
    return bool(ev and str(ev).strip())


def _parse_plan(text: str) -> dict:
    """Parse the model reply into a plan {edits: [...]}, KEEPING only well-formed,
    GROUNDED edits (an ungrounded or malformed edit is dropped — the eval-reject the
    spec calls for). Fail-open to {"edits": []} on any parse failure."""
    obj = extract_json(text)
    if not isinstance(obj, dict):
        return {"edits": []}
    raw = obj.get("edits", [])
    if not isinstance(raw, list):
        return {"edits": []}
    kept: list[dict] = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        if not e.get("old_id") or not e.get("new_body"):
            continue                          # a diff must target a unit + a new body
        if not _is_grounded(e):
            continue                          # UNGROUNDED → dropped (the eval-reject)
        kept.append({
            "verb": "auto_edit",
            "old_id": str(e["old_id"]),
            "new_body": str(e["new_body"]),
            "new_title": str(e.get("new_title", "")),
            "evidence": str(e["evidence"]),
        })
    return {"edits": kept}


def reflect(traces: list[dict], *, runner=None, model: str = REFLECT_MODEL,
            env=None) -> dict:
    """The GEPA-lite reflection (spec §5.1): ONE batched OAuth call through the
    OAuth chokepoint `run_claude` (the INJECTED runner is threaded into it) over all
    candidate traces → a parsed, grounded plan {edits: [...]}. NEVER spawns `claude`
    in a test (the runner is injected); the default runner (a real run only) is the
    OAuth subprocess. `env` (None → os.environ) is threaded to the chokepoint so a
    real cron run sanitizes the inherited env and a test can inject a fake OAuth env.

    FAIL-OPEN: a runner error / a non-zero exit / an unparseable reply / an OAuth
    violation degrades to an EMPTY plan ({"edits": []}) — never raises out into the
    maintenance run."""
    if not traces:
        return {"edits": []}
    runner = runner or default_runner()
    try:
        prompt = build_reflection_prompt(traces)
        out = call_model(prompt, system=_REFLECT_SYSTEM, runner=runner, model=model, env=env)
    except Exception:
        return {"edits": []}                  # fail-open
    return _parse_plan(out)


# --------------------------------------------------------------------------- #
# 3. Apply — redirect-preserving versioning, bounded, provenance-gated (the wall).
# --------------------------------------------------------------------------- #

def apply_edits(conn, admitted: list, *, ts: str, max_edits: int = MAX_EDITS
                ) -> list[dict]:
    """Apply the eval-ADMITTED edits via the wall's `apply_auto_edit` (redirect-
    preserving new version + superseded_by link carrying the trace refs as
    evidence). Each call funnels through assert_mutable (the wall RE-READS the live
    row) — provenance is enforced in the apply path, never the prompt.

    BOUNDED to `max_edits`, HALT-ON-EXCEED (§4c): an admitted set LARGER than the
    cap applies NONE of the class (not the first N) — a volume far over the cap is a
    signal something is wrong, so stop-and-ask. Returns the list of applied edits
    (each {old_id, new_id}) — empty when the bound halts.

    A ForbiddenTargetError from the wall PROPAGATES (the §4a zero-tolerance stop-the-
    world — the eval hard gate should have caught it first; reaching it here means a
    bug / injection, so it halts the whole apply, NOT a per-item skip). A PRE-FLIGHT
    pass re-reads EVERY target's live row via assert_mutable BEFORE any write — so a
    single forbidden target halts the batch with NOTHING written (not even the legal
    edits that preceded it in the list), the true zero-tolerance stop-the-world."""
    admitted = admitted if isinstance(admitted, list) else []
    # HALT-ON-EXCEED: do not even start if the batch is over the cap.
    if len(admitted) > max_edits:
        return []

    # Normalize the batch to the well-formed edits we will actually apply.
    batch: list[dict] = []
    for edit in admitted:
        if not isinstance(edit, dict) or not edit.get("old_id"):
            continue
        batch.append({
            "old_id": str(edit["old_id"]),
            "new_body": str(edit.get("new_body", "")),
            "new_title": str(edit.get("new_title", "") or ""),
            "evidence": str(edit.get("evidence", "") or ""),
        })

    # PRE-FLIGHT provenance check over the WHOLE batch (the §4a stop-the-world):
    # assert_mutable RE-READS each target's live row; a single forbidden target
    # raises HERE, before any write — so NOTHING in the batch is applied (not even
    # the legal edits earlier in the list). This is the zero-tolerance halt, not a
    # per-item skip.
    for edit in batch:
        assert_mutable(conn, MemoryUnit(edit["old_id"]))

    # All targets cleared the wall → apply the redirect-preserving versions.
    applied: list[dict] = []
    for edit in batch:
        new_id = apply_auto_edit(
            conn, old_id=edit["old_id"], new_body=edit["new_body"],
            new_title=edit["new_title"], evidence=edit["evidence"], ts=ts)
        applied.append({"old_id": edit["old_id"], "new_id": new_id})
    return applied


# --------------------------------------------------------------------------- #
# The track entry — eval-gate → bounded apply (the orchestrator calls this).
# --------------------------------------------------------------------------- #

def run_edit_track(conn, plan: dict, *, probes: list, embedder=None,
                   tmp_dir=None, ts: str, max_edits: int = MAX_EDITS,
                   apply: bool = True) -> dict:
    """Run the eval gate over a reflection plan, then (if `apply` and the run did
    not halt) apply the admitted edits within the bound + the wall.

    1. EVAL GATE (delegated): `run_aggressive_eval` runs the HARD gate (zero-
       tolerance provenance — a forbidden target sets halt=True, admits NOTHING) +
       the deterministic shadow QUALITY gate (a probe-regressing edit is rejected,
       kept in the digest, not applied).
    2. APPLY (only the admitted edits, only if not halted, only if `apply`): via
       `apply_edits` — bounded + redirect-preserving + provenance-gated by the wall.
       `apply=False` is the DRY-RUN path (spec §4f / §7 step 8): plan + eval +
       digest, apply NOTHING.

    Returns {halt, admitted, rejected, applied, forbidden_targets}. FAIL-OPEN: a
    malformed plan / an eval error degrades to a no-op result (applied=[]), never
    raises out into the maintenance run."""
    result = {"halt": False, "admitted": [], "rejected": [],
              "applied": [], "forbidden_targets": []}
    try:
        report = run_aggressive_eval(
            conn, plan, probes=probes, embedder=embedder, tmp_dir=tmp_dir, ts=ts,
            apply=apply)
    except Exception:
        return result                         # fail-open

    result["halt"] = bool(report.get("halt"))
    result["admitted"] = list(report.get("admitted", []) or [])
    result["rejected"] = list(report.get("rejected", []) or [])
    result["forbidden_targets"] = list(report.get("forbidden_targets", []) or [])

    # The §4a stop-the-world: a halted run applies NOTHING (not even legal edits).
    if result["halt"]:
        return result

    if not apply:
        return result                         # DRY-RUN: plan + eval, apply NOTHING

    # The admitted set is the apply-eligible edits; apply within the bound + wall.
    # (A ForbiddenTargetError here would be a bug — the hard gate already passed —
    #  so it is allowed to propagate as the zero-tolerance halt.)
    result["applied"] = apply_edits(
        conn, result["admitted"], ts=ts, max_edits=max_edits)
    return result
