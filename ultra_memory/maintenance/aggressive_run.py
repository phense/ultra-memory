"""SP-7 §4e/§5.5 + §7 step 8 — the AGGRESSIVE Stage-2c ORCHESTRATOR: wiring +
digest + DRY-RUN-FIRST.  The LAST build stage.

This module is the single entry the `run_maintenance.sh` Stage-2c block calls.  It
COMPOSES the safety wall (Stages 1-4) + the three aggressive tracks (Stages 5-7) —
it re-implements no guard.  In order, per the spec §5 pipeline diagram:

  0. THE GATE (§4f, `aggressive_bounds.run_gate`): read SP7_AGGRESSIVE_DISABLE /
     SP7_AGGRESSIVE_DRYRUN.  DISABLE → a NO-OP + one log line (SHIPS DISABLED — the
     cron env keeps DISABLE=1; the orchestrator runs the pass on-demand, fork C).
     DRYRUN → plan + eval + DIGEST, apply NOTHING.  LIVE → plan + eval + digest +
     bounded apply within the wall.
  1. PER-SKILL outcome_weight rates (the digest's first section) + the §5.2 EWMA
     aggregate fold (the no-LLM signal layer, `aggregate_all`).
  2. PLAN + APPLY the three tracks (each composes the wall + the bound + the eval):
       * edit      (§5.1, `aggressive_edit.run_edit_track`) — autonomous within the wall;
       * revert    (§5.2, `aggressive_revert.run_revert_track`) — PROPOSE-FOR-THE-OPERATOR
                    (fork A: apply=False ALWAYS in the autonomous pass; the operator confirms);
       * quarantine(§5.3, `aggressive_quarantine.run_quarantine_track`) — autonomous + gentle.
  3. PRE-RUN CHECKPOINT (§4d, `aggressive_bounds.pre_run_checkpoint`): a LIVE apply
     happens ONLY after a clean-tree git checkpoint of the repo + a memory_export
     snapshot.  A dirty tree (the 2026-05-24 untracked-files gap) → fail-soft SKIP:
     the LIVE pass applies NOTHING and the digest explains the skip.
  4. DIGEST (§4e): write `briefings/YYYY/sp7-self-improvement-YYYY-MM-DD.md` — the
     per-skill rates, edits proposed/applied/eval-rejected, the PROPOSED reversions
     (for the operator), the quarantine pairs (for the operator's adjudication), any bound-hit,
     and the EXACT one-command rollback (git reset + memory_import).  Plus a
     machine-audit jsonl row under briefings/maintenance-logs/sp7-*.jsonl.

THE WALL IS IN THE APPLY PATH (code), NEVER ONLY THE PROMPT (spec §4 design rule).
The tracks' apply paths funnel every write through `assert_mutable` (re-reads the
LIVE row, never trusts an LLM-echoed `created_by`/`pinned`); a single forbidden
target HALTS THE WHOLE RUN (§4a zero tolerance) — this orchestrator catches that
`ForbiddenTargetError`, marks the run halted, applies NOTHING, and records the halt
in the digest.

ZERO-TOLERANCE / BOUNDS / ARCHIVE-NEVER-DELETE / FAIL-OPEN are inherited from the
composed tracks; this module ADDS only the bounds gate over the WHOLE plan (so two
tracks' edits cannot together blow the per-period budget) and the digest.  It makes
NO LLM call of its own (the tracks own the ONE batched call each) and imports no
anthropic SDK (OAuth-only by construction; a guard test asserts it).  Every verb is
a reversible FSM transition via the tracks — NO rm / delete anywhere (asserted).

OAUTH-ONLY (HARD): the tracks' ONE batched call each routes through an INJECTED
runner (the `score_news.py` / `judge_borderline` precedent).  Tests inject a fake
runner + a stub embedder and NEVER spawn `claude` / NEVER load fastembed.

The engine primitives the tracks consume (`set_outcome_weight`, `set_status`,
`consolidate`, `save_memory`, `record_link`) are GENERIC + already on live master
(ffcd414).  The caps, the cadence, the trading-aware digest, and the
propose-for-the-operator reversion policy are the consumer's policy (e.g. a trading project).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The sibling SP-7 modules — the gate/bounds/checkpoint, the three tracks, the
# outcome aggregate.  This orchestrator COMPOSES them; it re-implements no guard.
from ultra_memory.maintenance import aggressive_bounds as ab  # noqa: E402
from ultra_memory.maintenance import aggressive_outcomes as ao  # noqa: E402
from ultra_memory.maintenance import aggressive_edit as aedit  # noqa: E402
from ultra_memory.maintenance import aggressive_revert as arev  # noqa: E402
from ultra_memory.maintenance import aggressive_quarantine as aq  # noqa: E402
from ultra_memory.maintenance.aggressive_wall import ForbiddenTargetError  # noqa: E402
from ultra_memory.maintenance.gate_commons import is_enabled_default_on  # noqa: E402

# The engine — generic, project-agnostic primitives (wiki_lib.py:24 precedent).
from ultra_memory import memory_lib  # noqa: E402

# The default briefings root (the §4e digest destination) + the machine-audit dir.
_DEFAULT_BRIEFINGS = None   # project-agnostic: the beat adapter passes config.briefings_dir
_DEFAULT_AUDIT = None       # derived from briefings_dir when set (None -> no audit file)

# The §4c per-period aggregate-cap budget (so two same-period runs cannot stack
# past these).  Conservative (fork B): generous-but-finite.  Period key = the
# YYYY-MM the run is in (monthly cadence — §5.5).
#
# BOTH the edit class AND the quarantine class are gated by their period cap in
# _run_tracks (plan → enforce_caps[period] → apply), so stacked re-runs cannot
# accumulate past the budget for EITHER class. The reversion class has NO active
# period cap by design: under fork A (propose-for-the-operator) the autonomous pass ALWAYS
# runs the revert track with apply=False, so reversions_applied is always [] and
# there is nothing to accumulate. The constant is kept (documented) so that IF a
# future decision flips reversion to autonomous-apply, the period gate is one
# enforce_caps(period_cap_reversions=...) call away — it is intentionally inert now,
# not an oversight.
_PERIOD_CAP_EDITS = 6
_PERIOD_CAP_REVERSIONS = 6        # inert under fork A (revert is always propose-only)
_PERIOD_CAP_QUARANTINES = 10


# --------------------------------------------------------------------------- #
# The run result — the digest + the audit payload.
# --------------------------------------------------------------------------- #

@dataclass
class RunResult:
    """Everything a run produced — the digest renders FROM this, and the audit row
    serializes a subset of it.  All collections default empty so a no-op/disabled
    run is a valid, render-able RunResult."""
    mode: str = "noop"                 # 'noop' | 'dryrun' | 'live'
    date: str = ""
    halt: bool = False                 # a §4a forbidden-target stop-the-world
    reason: str = ""                   # the gate's one-line note (for the digest)

    per_skill_rates: dict = field(default_factory=dict)
    evidence_coverage: dict | None = None   # §5.2 outcome-attribution coverage

    edits_proposed: list = field(default_factory=list)
    edits_applied: list = field(default_factory=list)
    edits_rejected: list = field(default_factory=list)

    proposed_reversions: list = field(default_factory=list)
    reversions_applied: list = field(default_factory=list)

    quarantine_pairs: list = field(default_factory=list)
    merged_pairs: list = field(default_factory=list)

    bounds_hit: list = field(default_factory=list)
    forbidden_targets: list = field(default_factory=list)

    applied_counts: dict = field(
        default_factory=lambda: {"edits": 0, "reversions": 0, "quarantines": 0})

    checkpoint: object = None          # the CheckpointResult (or None if not run)
    rollback_command: str = ""
    digest_path: str | None = None


# --------------------------------------------------------------------------- #
# Per-skill outcome_weight rates (the digest's first section, §4e).
# --------------------------------------------------------------------------- #

def per_skill_outcome_rates(conn, *, override_weights: dict | None = None) -> dict:
    """Group agent-authored active units by their `index_hook` (the skill tag) and
    average their `outcome_weight`.  Units with no index_hook fold under
    'unattributed'.  Fail-open-to-empty on a read error.

    FIX 5: `override_weights` (the DRY-RUN would-be weights captured by the no-op
    set_weight_fn) overlays the on-disk `outcome_weight` per id, so a dry-run digest
    reports the SAME per-skill rates a live run WOULD have produced — WITHOUT having
    persisted any demotion. In a live run override_weights is None (the on-disk
    value, just written, is the truth)."""
    try:
        rows = conn.execute(
            "SELECT id, index_hook, outcome_weight FROM memories "
            "WHERE created_by IN ('agent','background_review') "
            "AND status='active'"
        ).fetchall()
    except Exception:
        return {}
    overrides = override_weights or {}
    acc: dict = {}
    for r in rows:
        skill = r["index_hook"] or "unattributed"
        w = overrides.get(r["id"], r["outcome_weight"])
        if w is None:
            continue
        acc.setdefault(skill, []).append(float(w))
    return {k: (sum(v) / len(v)) for k, v in acc.items() if v}


# --------------------------------------------------------------------------- #
# §5.2 outcome-attribution COVERAGE (the digest's calibration section).
#
# The self-reversion signal + the per-skill rates are only as meaningful as the
# outcome-attribution graph is populated. In this build the graph is fed by the
# SINGLE graduation `validated_as` edge per unit (consolidate_candidates.py); there
# is no downstream re-attribution of NEW outcomes to a graduated unit yet (the
# SP-6 outcome-attribution-backfill is the upstream substrate dependency the spec
# gates SP-7 on). Until that backfill exists, most units sit BELOW MIN_EVIDENCE, so
# is_regression returns False (sub-floor) and the edit/reversion tracks are largely
# inert. This section makes that EXPLICIT so a near-empty dry-run digest is NOT
# misread as "the loop sees nothing wrong" when it actually had near-zero evidence
# to reason over.
# --------------------------------------------------------------------------- #

def _informed_by_count(conn, unit_id) -> int:
    """Count a unit's REAL usage-outcome edges specifically — the `informed_by`
    edges SP-8 writes at session-end (vs the loop's own bookkeeping `validated_as` /
    `superseded_by`). Mirrors `ao.linked_outcomes`' join but filters to
    predicate='informed_by'. Fail-open-to-0 per unit (never raises)."""
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
              FROM links l
              JOIN session_events se ON se.id = CAST(l.src_id AS INTEGER)
             WHERE l.dst_kind = 'memory'
               AND l.dst_id = ?
               AND l.src_kind = 'session_event'
               AND l.predicate = 'informed_by'
               AND se.outcome_signal IS NOT NULL
            """,
            (unit_id,),
        ).fetchone()
    except Exception:
        return 0
    return int(row["n"]) if row is not None else 0


def outcome_evidence_coverage(conn) -> dict:
    """Summarize how much outcome evidence the loop actually has to reason over.

    Counts the agent-authored active units and, for each, how many linked outcome
    events it carries (via `ao.linked_outcomes` — which AFTER the SP-8 fold includes
    `informed_by` usage edges). Returns
    {total_units, with_outcomes, below_floor, at_or_above_floor, min_evidence,
     with_usage_outcomes, usage_at_or_above_floor}:

      * `with_outcomes` / `at_or_above_floor` count ANY outcome edge (bookkeeping +
        usage) — `at_or_above_floor` is the count the regression/edit tracks can act on.
      * `with_usage_outcomes` / `usage_at_or_above_floor` count specifically the
        `informed_by` REAL-usage edges (SP-8), so the operator can tell loop-bookkeeping
        evidence from real-usage evidence.

    Fail-open-to a minimal dict on a read error (never raises)."""
    cov = {"total_units": 0, "with_outcomes": 0, "below_floor": 0,
           "at_or_above_floor": 0, "min_evidence": ao.MIN_EVIDENCE,
           "with_usage_outcomes": 0, "usage_at_or_above_floor": 0}
    try:
        rows = conn.execute(
            "SELECT id FROM memories "
            "WHERE created_by IN ('agent','background_review') "
            "AND status='active' AND pinned=0"
        ).fetchall()
    except Exception:
        return cov
    cov["total_units"] = len(rows)
    for r in rows:
        try:
            n = len(ao.linked_outcomes(conn, r["id"]))
        except Exception:
            n = 0
        if n > 0:
            cov["with_outcomes"] += 1
        if n >= ao.MIN_EVIDENCE:
            cov["at_or_above_floor"] += 1
        else:
            cov["below_floor"] += 1
        # The informed_by-only (real-usage) breakdown — fail-open to 0 per unit.
        usage_n = _informed_by_count(conn, r["id"])
        if usage_n > 0:
            cov["with_usage_outcomes"] += 1
        if usage_n >= ao.MIN_EVIDENCE:
            cov["usage_at_or_above_floor"] += 1
    return cov


# --------------------------------------------------------------------------- #
# The digest path + renderer (§4e).
# --------------------------------------------------------------------------- #

def digest_path_for(briefings_dir, date: str) -> Path:
    """briefings/<YYYY>/sp7-self-improvement-<YYYY-MM-DD>.md (spec §4e)."""
    year = date[:4]
    return Path(briefings_dir) / year / f"sp7-self-improvement-{date}.md"


def _banner(mode: str) -> str:
    if mode == "live":
        return "LIVE — aggressive actions were APPLIED within the wall + bounds."
    if mode == "dryrun":
        return "DRY-RUN — PROPOSED actions only; APPLIED NOTHING (the operator reviews this)."
    return "NO-OP — the aggressive pass is DISABLED (no plan, no apply)."


def _env_truthy(name: str) -> bool:
    """The SP-8 ships-disabled truthy reader (the same convention the Stop hook +
    config.py use): unset / blank / '0' / 'false' ⇒ OFF; '1'/'true'/'yes' ⇒ ON."""
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes")


def _attribution_policy_in_force() -> tuple[str, int, bool]:
    """The SP-8 attribution policy currently in force, read from the env (the single
    runtime source of truth, mirrored in config.py): (policy, k, enabled). Defaults
    'top_k' / 1 / ON; a bad SP8_ATTRIBUTION_K ⇒ 1."""
    policy = (os.environ.get("SP8_ATTRIBUTION_POLICY") or "top_k").strip() or "top_k"
    try:
        k = int(os.environ.get("SP8_ATTRIBUTION_K") or "1")
    except Exception:
        k = 1
    # Opt-OUT: attribution is ON by default; disable with SP8_ATTRIBUTION_ENABLE=0.
    return policy, k, is_enabled_default_on("SP8_ATTRIBUTION_ENABLE")


def render_digest(rr: RunResult) -> str:
    """Render the §4e human meta-learning digest from a RunResult.  Markdown with:
    a clear LIVE/DRY-RUN/NO-OP banner, per-skill outcome_weight rates, the edits
    proposed/applied/eval-rejected, the PROPOSED reversions (for the operator), the
    quarantine pairs (for the operator's adjudication), any bound-hit, the halt state, and
    the EXACT one-command rollback."""
    L: list[str] = []
    L.append(f"# SP-7 self-improvement digest — {rr.date}")
    L.append("")
    L.append(f"**Mode:** {_banner(rr.mode)}")
    if rr.reason:
        L.append("")
        L.append(f"> {rr.reason}")
    if rr.halt:
        L.append("")
        L.append("## ⛔ RUN HALTED (§4a stop-the-world)")
        # The §4a contract is "the HALTING action applied nothing" — but engine
        # verbs auto-commit per verb (no enclosing txn), so an EARLIER track may
        # already have committed writes before a LATER track raised the stop-the-
        # world. Report ACCURATELY: only claim "NOTHING was applied" when nothing
        # actually landed; otherwise name the partial apply + point at the rollback.
        applied_total = sum(int(v) for v in (rr.applied_counts or {}).values())
        L.append("A proposed action targeted a forbidden (human/import/pinned) unit "
                 "— the zero-tolerance hard gate HALTED the whole run. This is a bug "
                 "or a prompt-injection; review below.")
        if applied_total == 0:
            L.append("")
            L.append("**NOTHING was applied** — the halt fired before (or at) the "
                     "first write of the batch.")
        else:
            L.append("")
            L.append(f"**⚠ PARTIAL APPLY — {applied_total} action(s) from an EARLIER "
                     f"track had ALREADY committed before the halt** (engine verbs "
                     f"auto-commit per verb; there is no whole-run transaction). The "
                     f"halting action itself applied nothing, but the landed writes "
                     f"below are real. **Roll back via the checkpoint command at the "
                     f"bottom of this digest** to undo them.")
            L.append("")
            L.append(f"  - edits applied before halt: "
                     f"**{rr.applied_counts.get('edits', 0)}**")
            L.append(f"  - reversions applied before halt: "
                     f"**{rr.applied_counts.get('reversions', 0)}**")
            L.append(f"  - quarantine pairs applied before halt: "
                     f"**{rr.applied_counts.get('quarantines', 0)}**")
        if rr.forbidden_targets:
            L.append("")
            L.append("Forbidden targets attempted:")
            for t in rr.forbidden_targets:
                L.append(f"  - `{t}`")
    if rr.checkpoint is not None and not getattr(rr.checkpoint, "ok", True):
        L.append("")
        L.append("## ⚠ CHECKPOINT SKIPPED — applied nothing (§4d)")
        L.append(f"The pre-run checkpoint could not be made cleanly, so the LIVE "
                 f"pass was SKIPPED (fail-soft). Reason: "
                 f"{getattr(rr.checkpoint, 'reason', 'unknown')}")

    # --- applied / proposed summary ---------------------------------------- #
    L.append("")
    L.append("## Summary")
    ac = rr.applied_counts
    L.append(f"- edits: proposed **{len(rr.edits_proposed)}**, "
             f"applied **{ac.get('edits', 0)}**, "
             f"eval-rejected **{len(rr.edits_rejected)}**")
    L.append(f"- reversions: proposed **{len(rr.proposed_reversions)}**, "
             f"applied **{ac.get('reversions', 0)}** "
             f"(propose-for-the-operator — the autonomous pass applies NONE)")
    L.append(f"- quarantine pairs: applied **{ac.get('quarantines', 0)}**, "
             f"merged (duplicates) **{len(rr.merged_pairs)}**")

    # --- per-skill outcome_weight rates ------------------------------------ #
    L.append("")
    L.append("## Per-skill outcome_weight rates")
    if rr.per_skill_rates:
        for skill in sorted(rr.per_skill_rates):
            L.append(f"- `{skill}`: {rr.per_skill_rates[skill]:.2f}")
    else:
        L.append("- (no agent-authored units with an outcome_weight yet)")

    # --- outcome-attribution coverage (§5.2 — calibrate the dry-run) -------- #
    # Without this section a near-empty digest reads like a clean bill of health.
    # In fact the edit/reversion tracks can only act on units AT-OR-ABOVE
    # MIN_EVIDENCE. SP-8 supplies the REAL usage-outcome attribution (the `informed_by`
    # edges written at session-end) that the loop folds into its signal — so we now
    # break the coverage into loop-bookkeeping evidence (any outcome edge) vs real
    # USAGE evidence (informed_by), and surface the attribution policy in force, so
    # the digest is read correctly (and an empty one is not misread as "all clear").
    L.append("")
    L.append("## Outcome-attribution coverage (§5.2)")
    ev = rr.evidence_coverage
    if ev:
        floor = ev.get("min_evidence", "?")
        usage_any = ev.get("with_usage_outcomes", 0)
        usage_floor = ev.get("usage_at_or_above_floor", 0)
        policy, k, enabled = _attribution_policy_in_force()
        L.append(f"- agent-authored active units: **{ev.get('total_units', 0)}**")
        L.append(f"- with ANY linked outcome: **{ev.get('with_outcomes', 0)}**")
        L.append(f"- at/above MIN_EVIDENCE (={floor}, the loop CAN act): "
                 f"**{ev.get('at_or_above_floor', 0)}**")
        L.append(f"- BELOW MIN_EVIDENCE (sub-floor, the loop must NOT act): "
                 f"**{ev.get('below_floor', 0)}**")
        L.append(f"- with USAGE attribution (informed_by ≥1): **{usage_any}**")
        L.append(f"- usage at/above MIN_EVIDENCE: **{usage_floor}**")
        L.append(f"- attribution policy in force: **{policy}**, k=**{k}** "
                 f"(SP8_ATTRIBUTION_ENABLE={'on' if enabled else 'off'})")
        # The narrative note: SP-8 now provides the usage attribution, so calibrate by
        # whether the gate is armed and whether any usage outcome has landed yet.
        if not enabled:
            L.append("")
            L.append("> ⚠ **Usage-attribution DISABLED:** SP8_ATTRIBUTION_ENABLE is "
                     "OFF (ships-disabled default), so `informed_by` usage evidence "
                     "stays **0 by design** — no session attributes its outcome to the "
                     "memories it recalled. Read an empty actions list as *the loop "
                     "had almost nothing to reason over* (arm the gate for a dry-run "
                     "cycle to populate usage attribution), NOT as *the loop saw "
                     "nothing wrong*.")
        elif usage_any == 0:
            L.append("")
            L.append("> ⚠ **No usage attribution yet:** the gate is ARMED but NO "
                     "session has yet attributed a usage outcome (`informed_by`) to a "
                     "recalled memory — coverage is **accruing**. Read an empty actions "
                     "list as *the loop is still gathering real-usage evidence*, NOT "
                     "as *the loop saw nothing wrong*.")
        elif ev.get("at_or_above_floor", 0) == 0:
            L.append("")
            L.append("> ⚠ **Near-zero evidence:** usage attribution is accruing but NO "
                     "unit has reached MIN_EVIDENCE. The regression + edit signals are "
                     "SUB-FLOOR — read an empty actions list as *the loop had almost "
                     "nothing to reason over*, NOT as *the loop saw nothing wrong*.")
    else:
        L.append("- (evidence coverage not computed for this run)")

    # --- the edits (proposed / applied / rejected) ------------------------- #
    L.append("")
    L.append("## Auto-edits")
    if rr.edits_applied:
        L.append("Applied:")
        for e in rr.edits_applied:
            L.append(f"  - {json.dumps(e, ensure_ascii=False)}")
    if rr.edits_rejected:
        L.append("Eval-rejected (kept here, NOT applied — a probe regression):")
        for e in rr.edits_rejected:
            L.append(f"  - {json.dumps(e, ensure_ascii=False)}")
    if not rr.edits_applied and not rr.edits_rejected:
        L.append("- (none)")

    # --- the PROPOSED reversions (for the operator — fork A) --------------- #
    L.append("")
    L.append("## Proposed reversions — FOR THE OPERATOR (propose-for-the-operator, fork A)")
    L.append("Reverting is the verb most likely to be itself wrong (a regression "
             "may be noise). The loop PROPOSES; **the operator confirms** before any "
             "reversion lands.")
    if rr.proposed_reversions:
        for p in rr.proposed_reversions:
            rid = p.get("regressed_id")
            pid = p.get("prior_id")
            L.append(f"  - regressed `{rid}` ← revert to prior `{pid}` "
                     f"({json.dumps(p.get('evidence', {}), ensure_ascii=False)})")
    else:
        L.append("- (none)")

    # --- the quarantine pairs (for the operator's adjudication) ------------ #
    L.append("")
    L.append("## Quarantine pairs — FOR THE OPERATOR's adjudication")
    L.append("Two agent-authored units that DISAGREE were demoted out of recall "
             "(both quarantined, nothing edited/deleted — fully reversible). The "
             "loop does NOT pick a winner; the operator adjudicates.")
    if rr.quarantine_pairs:
        for p in rr.quarantine_pairs:
            L.append(f"  - `{p.get('id_a')}`  ⟷  `{p.get('id_b')}`")
    else:
        L.append("- (none)")

    # --- bound-hits -------------------------------------------------------- #
    L.append("")
    L.append("## Bound-hits (§4c)")
    if rr.bounds_hit:
        L.append("A class proposed MORE actions than its cap → halt-on-exceed "
                 "(applied NONE of that class — a volume far over the cap is itself "
                 "a signal something is wrong):")
        for b in rr.bounds_hit:
            L.append(f"  - {ab.format_bound_message(b)}")
    else:
        L.append("- (no bound hit)")

    # --- the EXACT rollback command (§4d) ---------------------------------- #
    L.append("")
    L.append("## Rollback — the EXACT one command (§4d)")
    L.append("```")
    L.append(rr.rollback_command or "(no checkpoint was made — nothing to roll back)")
    L.append("```")
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# The audit jsonl row (the machine-audit half of §4e).
# --------------------------------------------------------------------------- #

def _write_audit_row(audit_dir, rr: RunResult, ts: str) -> None:
    """Append a machine-audit row to briefings/maintenance-logs/sp7-<date>.jsonl.
    Best-effort — a write failure is swallowed (the digest is the human record;
    this is the machine mirror).  No secrets ride here (counts + ids only)."""
    if audit_dir is None:
        return                                  # pure-memory install: no audit dir
    try:
        d = Path(audit_dir)
        d.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": ts,
            "wrapper": "sp7-aggressive",
            "mode": rr.mode,
            "halt": rr.halt,
            "applied_counts": rr.applied_counts,
            "edits_proposed": len(rr.edits_proposed),
            "edits_rejected": len(rr.edits_rejected),
            "proposed_reversions": len(rr.proposed_reversions),
            "quarantine_pairs": len(rr.quarantine_pairs),
            "bounds_hit": rr.bounds_hit,
            "forbidden_targets": rr.forbidden_targets,
            "checkpoint_ok": (getattr(rr.checkpoint, "ok", None)
                              if rr.checkpoint is not None else None),
            "digest": rr.digest_path,
        }
        with (d / f"sp7-{rr.date}.jsonl").open("a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass                                    # fail-open: audit is best-effort


def _write_digest(briefings_dir, rr: RunResult) -> str | None:
    """Render + write the §4e digest.  Returns the path on success, None on a
    write failure (fail-open — the digest is best-effort, never a wedge)."""
    if briefings_dir is None:
        return None                             # pure-memory install: no digest dir
    try:
        path = digest_path_for(briefings_dir, rr.date)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_digest(rr))
        return str(path)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# The Stage-2c entry — the single function run_maintenance.sh calls.
# --------------------------------------------------------------------------- #

def run_aggressive_pass(conn, *, repo_root, date: str, ts: str, probes: list,
                        embedder=None, runner=None,
                        briefings_dir=_DEFAULT_BRIEFINGS,
                        audit_dir=None, git_env=None, export_fn=None,
                        include_graduations: bool = False,
                        period: str | None = None,
                        oauth_env=None,
                        log=lambda _m: None) -> RunResult:
    """Run the SP-7 aggressive Stage-2c pass (spec §4e/§5.5 + §7 step 8).

    GATED + SHIPS DISABLED: reads the kill switch / dry-run flag FIRST
    (`aggressive_bounds.run_gate`).  DISABLE → a no-op (the cron env keeps DISABLE
    set; the orchestrator runs the pass on-demand).  DRYRUN → plan + eval + digest,
    apply NOTHING.  LIVE → plan + eval + digest + bounded apply within the wall,
    AFTER a clean-tree pre-run checkpoint.

    FAIL-OPEN: any error degrades to a safe no-op RunResult + one log line — it
    NEVER raises out into the maintenance run.  A ForbiddenTargetError from a track
    (the §4a stop-the-world) is caught here → the run is marked halted, NOTHING is
    applied, and the digest records the halt.

    Every LLM call routes through the tracks' INJECTED `runner` (OAuth-only; tests
    inject a fake).  The `embedder` is injected (tests stub it; never fastembed).
    `export_fn` is supplied by the orchestrator so this module stays free of the
    live-store export coupling.
    """
    rr = RunResult(date=date)
    period = period or date[:7]                 # YYYY-MM (monthly cadence)
    # Project-agnostic: with no briefings_dir there is no digest/audit destination
    # (a pure-memory install) — derive None, and _write_digest/_write_audit_row no-op.
    audit_dir = audit_dir or (
        (Path(briefings_dir) / "maintenance-logs") if briefings_dir else None)

    try:
        # --- 0. THE GATE (§4f) — kill switch / dry-run ---------------------- #
        gate = ab.run_gate(log=log)
        rr.mode = gate.mode
        rr.reason = gate.reason
        if gate.mode == "noop":
            # DISABLED (or errored) → a true no-op: no plan, no digest, no audit.
            return rr

        live = gate.may_apply                    # True ONLY in 'live'

        # --- 1. per-skill rates + the §5.2 EWMA aggregate fold -------------- #
        # FIX 5: aggregate_all → set_outcome_weight is a real COMMITTED UPDATE of
        # memories.outcome_weight (it demotes recall rank). A dry-run must apply
        # NOTHING — so in dry-run we pass a NO-OP set_weight_fn that captures the
        # would-be weights IN-MEMORY (for an honest digest) without persisting any
        # demotion. Only a LIVE run calls the real set_outcome_weight. (This runs
        # BEFORE the checkpoint, so `live` here is the gate's intent; a later
        # dirty-tree downgrade of `live` only affects the APPLY tracks, never the
        # already-skipped aggregate — which is correct: a dirty-tree dry-run also
        # persists nothing.)
        would_be_weights: dict = {}

        def _dryrun_set_weight(_conn, *, id, weight, ts, reason):  # noqa: A002
            would_be_weights[id] = weight        # capture only — NO write

        try:
            if live:
                ao.aggregate_all(conn, ts=ts)    # LIVE: the real committed write
            else:
                ao.aggregate_all(conn, ts=ts, set_weight_fn=_dryrun_set_weight)
        except Exception:
            pass                                 # fail-open: keep prior weights
        rr.per_skill_rates = per_skill_outcome_rates(
            conn, override_weights=(None if live else would_be_weights))
        try:
            rr.evidence_coverage = outcome_evidence_coverage(conn)
        except Exception:
            rr.evidence_coverage = None          # fail-open: coverage is best-effort

        # --- 2. PRE-RUN CHECKPOINT (§4d) — only when we intend to APPLY ----- #
        # The checkpoint is a LIVE-apply anchor; dry-run plans without writing, so
        # it needs no checkpoint.  A dirty tree → fail-soft: drop to plan-only.
        checkpoint = None
        if live:
            checkpoint = ab.pre_run_checkpoint(
                repo_root=repo_root, date=date,
                export_fn=(export_fn or (lambda: None)), env=git_env)
            rr.checkpoint = checkpoint
            rr.rollback_command = checkpoint.rollback_command
            if not checkpoint.ok:
                # The 2026-05-24 untracked-files gap: refuse to apply on a dirty /
                # un-checkpointable tree.  Degrade to plan-only (apply NOTHING).
                log(f"SP-7 checkpoint not made ({checkpoint.reason}) — skipping the "
                    f"aggressive APPLY (plan-only).")
                live = False

        # --- 3. PLAN + APPLY the three tracks ------------------------------ #
        # Each track composes the wall + its bound + the eval.  `apply` is False in
        # dry-run / on a skipped checkpoint (plan-only).  A ForbiddenTargetError
        # from any track's apply path is the §4a stop-the-world (caught below).
        _run_tracks(conn, rr, probes=probes, embedder=embedder, runner=runner, ts=ts,
                    apply=live, include_graduations=include_graduations,
                    period=period, oauth_env=oauth_env)

    except ForbiddenTargetError as exc:
        # The §4a zero-tolerance stop-the-world: a forbidden target reached the apply
        # path (the hard gate should have caught it — reaching here means a bug /
        # injection).  Mark halted; the HALTING track applied NOTHING (a track raises
        # the stop-the-world from its PRE-FLIGHT, before the first write of ITS
        # batch).
        #
        # DO NOT blanket-zero applied_counts: engine verbs auto-commit per verb
        # (BEGIN IMMEDIATE/COMMIT, no enclosing whole-run txn), so an EARLIER track
        # (e.g. the edit track) may ALREADY have committed writes before a LATER
        # track raised the stop-the-world. _run_tracks records each track's real
        # applied count into rr.applied_counts AS IT COMPLETES; we keep those here so
        # the digest/audit report the LANDED writes accurately (the §4a "nothing
        # applied" contract is about the HALTING action, not the whole run — and the
        # digest's halt section now distinguishes the two and points at the rollback).
        rr.halt = True
        rr.forbidden_targets = list(getattr(exc, "targets", []) or [str(exc)])
        log(f"SP-7 aggressive pass HALTED — forbidden target (§4a): {exc!r}")
        if any(rr.applied_counts.values()):
            log(f"SP-7 HALT after a partial apply — {rr.applied_counts} ALREADY "
                f"committed by an earlier track; roll back via the pre-run checkpoint.")
    except Exception as exc:
        # Total fail-open: any other error → a safe no-op result + one line.
        log(f"SP-7 aggressive pass errored — degrading to no-op ({exc!r}).")
        # keep whatever partial counts were set; ensure applied stays zeroed-safe
        # if an error interrupted mid-apply is impossible (tracks are atomic-per-verb).

    # --- 4. DIGEST (§4e) + the machine-audit row --------------------------- #
    rr.digest_path = _write_digest(briefings_dir, rr)
    _write_audit_row(audit_dir, rr, ts)
    return rr


def _run_tracks(conn, rr: RunResult, *, probes, embedder, runner, ts, apply,
                include_graduations, period, oauth_env=None) -> None:
    """Run the three tracks and fold their results into `rr`.  Applies the §4c
    global per-period aggregate cap over the WHOLE plan (so two tracks' actions in
    a period cannot together exceed the period budget) on top of each track's own
    per-run cap.

    The reversion track is ALWAYS propose-only in the autonomous pass (fork A:
    propose-for-the-operator); only `apply=True` LETS the edit + quarantine tracks write.
    """
    # --- edit track (§5.1) — autonomous within the wall -------------------- #
    # Build the reflection plan first (the no-LLM candidate select → the ONE batched
    # reflection call), then run the eval+apply.  All bounded + provenance-gated.
    try:
        candidates = aedit.select_edit_candidates(conn)
        traces = [aedit.build_trace(conn, c["unit_id"], dup_id=c.get("dup_id"))
                  for c in candidates]
        plan = (aedit.reflect(traces, runner=runner, env=oauth_env)
                if traces else {"edits": []})
    except Exception:
        plan = {"edits": []}

    # §4c global per-period cap over the edit plan (additive to the per-run cap).
    cap = ab.enforce_caps(
        {"edits": plan.get("edits", [])}, conn=conn, period=period,
        period_cap_edits=_PERIOD_CAP_EDITS)
    rr.bounds_hit.extend(cap.bounds_hit)
    plan = {"edits": cap.admitted.get("edits", [])}
    rr.edits_proposed = list(plan.get("edits", []))

    edit_res = aedit.run_edit_track(
        conn, plan, probes=probes, embedder=embedder, ts=ts, apply=apply)
    if edit_res.get("halt"):
        # The eval hard gate caught a forbidden target → raise the stop-the-world so
        # the orchestrator's except-arm marks the run halted (NOTHING applied).
        forbidden = edit_res.get("forbidden_targets") or ["edit-track forbidden target"]
        exc = ForbiddenTargetError(f"edit-track hard gate failed: {forbidden}")
        exc.targets = list(forbidden)
        raise exc
    rr.edits_applied = list(edit_res.get("applied", []))
    rr.edits_rejected = list(edit_res.get("rejected", []))
    rr.applied_counts["edits"] = len(rr.edits_applied)
    # FIX 4: surface the empty-probe fail-closed hold in the digest so the
    # operator sees WHY no edits were admitted (vs an empty plan).
    for note in (edit_res.get("notes") or []):
        rr.bounds_hit.append(note)

    # --- revert track (§5.2, fork A) — PROPOSE-FOR-PETER (apply=False always) #
    rev_res = arev.run_revert_track(
        conn, ts=ts, apply=False, include_graduations=include_graduations)
    rr.proposed_reversions = list(rev_res.get("proposed", []))
    rr.reversions_applied = list(rev_res.get("applied", []))   # always [] (propose-only)
    rr.applied_counts["reversions"] = len(rr.reversions_applied)

    # --- quarantine track (§5.3) — autonomous + gentle --------------------- #
    # The §4c global per-period aggregate cap applies to QUARANTINES too, not only
    # edits (the runbook §5.3 + spec §4c claim the period cap covers stacked re-runs
    # across ALL classes). We PLAN the track (dry-run: pre-filter + adjudicate, no
    # write) to learn the proposed contradicts/merges, gate them through enforce_caps
    # for the PERIOD scope (additive to the track's own per-run cap), and only THEN
    # apply the admitted pairs. This mirrors the edit-track plan→cap→apply pattern so
    # a third same-period run cannot push quarantines past the period budget.
    #
    # A ForbiddenTargetError on the apply path propagates as the §4a stop-the-world.
    q_plan = aq.run_quarantine_track(
        conn, ts=ts, embedder=embedder, runner=runner, apply=False, env=oauth_env)
    proposed_q = list(q_plan.get("quarantined", []))     # contradicts pairs
    proposed_m = list(q_plan.get("merged", []))          # duplicate merges

    # §4c PERIOD cap over the quarantine plan (halt-on-exceed). The duplicate-merge
    # route is the conservative SP-6 verb, not a quarantine, so it is NOT counted
    # against the quarantine period budget — only the contradicts pairs are.
    q_cap = ab.enforce_caps(
        {"quarantines": proposed_q}, conn=conn, period=period,
        period_cap_quarantines=_PERIOD_CAP_QUARANTINES)
    rr.bounds_hit.extend(q_cap.bounds_hit)
    admitted_q = list(q_cap.admitted.get("quarantines", []))

    if not apply:
        # DRY-RUN: surface the PROPOSED (period-admitted) pairs without applying.
        rr.quarantine_pairs = admitted_q
        rr.merged_pairs = proposed_m
        rr.applied_counts["quarantines"] = 0
    else:
        # APPLY only the period-admitted contradicts pairs + the duplicate merges,
        # through the wall + the per-run bound (a ForbiddenTargetError propagates as
        # the §4a stop-the-world). The dry-run `merged` shape is
        # {canonical_id, loser_id}; `apply_merges` keeps id_a=canonical, id_b=loser,
        # so re-key it back into that pair shape.
        rr.quarantine_pairs = aq.apply_quarantines(conn, admitted_q, ts=ts)
        merge_pairs = [{"id_a": m.get("canonical_id"), "id_b": m.get("loser_id")}
                       for m in proposed_m
                       if m.get("canonical_id") and m.get("loser_id")]
        rr.merged_pairs = aq.apply_merges(conn, merge_pairs, ts=ts)
        rr.applied_counts["quarantines"] = len(rr.quarantine_pairs)

    # --- §4c period-budget bookkeeping — record what was actually applied --- #
    if apply:
        try:
            ab.commit_period_usage(
                conn, period=period,
                applied={"edits": rr.edits_applied,
                         "reversions": rr.reversions_applied,
                         "quarantines": rr.quarantine_pairs},
                ts=ts)
        except Exception:
            pass                                # fail-open: the meta KV is non-critical


# --------------------------------------------------------------------------- #
# CLI — the run_maintenance.sh Stage-2c entry (a thin shell over the library).
# --------------------------------------------------------------------------- #

def _default_export_fn(conn, repo_root: str, ts: str):
    """The real-run memory_export snapshot (imported LAZILY so the module imports
    clean + tests never touch it).  Writes the export view under
    <repo_root>/data/memory_export/ — the §4d recoverability snapshot that pairs
    with the pre-run git tag.  A failure RAISES (the checkpoint then fail-soft skips
    → the orchestrator degrades to plan-only)."""
    def _export():
        from ultra_memory import memory_export  # noqa: F401
        out_dir = Path(repo_root) / "data" / "memory_export"
        memory_export.export_memory(conn, str(out_dir), ts=ts, snapshot=True)
    return _export


def _resolve_embedder():
    """The engine's fastembed-backed default embedder, or None (the quarantine +
    eval tracks' memory-side recall then sees no near-pairs — fail-open, consistent
    with the rest of the pipeline)."""
    from ultra_memory import retrieval_core
    try:
        return retrieval_core.default_embedder()
    except Exception as exc:  # fastembed absent → degrade (no memory-side recall)
        sys.stderr.write(f"note: aggressive: no embedder ({exc}); memory-side recall "
                         f"degraded (fail-open)\n")
        return None


# --------------------------------------------------------------------------- #
# Registry adapter — the beat signature run_pipeline calls (the self-correct beat).
# --------------------------------------------------------------------------- #

def beat(conn, config, ts, env):
    """The `run_pipeline` registry entry for the AGGRESSIVE (self-correct) beat.

    Threads the config seam (project_dir / briefings_dir / export_dir) into
    run_aggressive_pass. GOVERNED — not ships-disabled here (the north-star
    active-on-install posture): the safety is the WALL (provenance gate + bounded
    blast radius + the eval hard-gate + archive-never-delete) + the SP7_AGGRESSIVE_*
    run_gate (read from the process env) + a clean-tree git checkpoint required
    before any LIVE apply + the empty-evidence floor (inert until outcome attribution
    is armed). With no retrieval probe set (`probes=[]`) the edit track holds
    fail-closed (no auto-edits) — the conservative default; the revert track is
    always propose-for-the-operator; only the gentle, reversible quarantine track can act
    autonomously, and only on contradictory near-pairs with a real embedder."""
    date = ts[:10]

    def _export():
        from ultra_memory import memory_export
        memory_export.export_memory(conn, str(config.export_dir), ts=ts, snapshot=True)

    return run_aggressive_pass(
        conn, repo_root=str(config.project_dir), date=date, ts=ts, probes=[],
        embedder=_resolve_embedder(), runner=None,
        briefings_dir=config.briefings_dir, export_fn=_export,
        git_env=env, oauth_env=env, period=date[:7],
        log=lambda m: sys.stderr.write(f"[aggressive] {m}\n"))


def main(argv=None) -> int:
    """Run the aggressive Stage-2c pass against a real store (the on-demand path).

    DEFENSE: this CLI is the path the orchestrator runs ON-DEMAND.  The cron env
    keeps SP7_AGGRESSIVE_DISABLE=1, so a cron invocation is a guaranteed no-op; an
    operator runs `--db ... ` with DISABLE unset (live) or with DRYRUN set.  Exits 0
    ALWAYS (fail-open — never wedge the maintenance run)."""
    import argparse
    import datetime

    ap = argparse.ArgumentParser(description="SP-7 aggressive Stage-2c pass.")
    ap.add_argument("--db", required=True, help="memory.db path")
    ap.add_argument("--repo-root", required=True, help="repo root for the checkpoint")
    ap.add_argument("--briefings-dir", default=str(_DEFAULT_BRIEFINGS))
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--probes", default=None,
                    help="path to a frozen retrieval probe-set JSON (optional)")
    args = ap.parse_args(argv)

    date = args.date or datetime.date.today().isoformat()
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Read the frozen probe set if supplied (the §6 quality-gate fixture).
    probes = []
    if args.probes:
        try:
            probes = json.loads(Path(args.probes).read_text())
        except Exception:
            probes = []

    try:
        conn = memory_lib.open_memory_db(args.db)
    except Exception as exc:
        print(f"sp7-aggressive: cannot open db {args.db}: {exc!r} — no-op.")
        return 0                                 # fail-open

    def _log(m):
        print(m)

    # The memory-side recall the quarantine (select_near_pairs) + eval
    # (_probe_recall_ids) tracks do REQUIRES a real embedder — the engine's BM25
    # fail-open is knowledge-side only, so threading embedder=None silently empties
    # the quarantine candidates and makes the eval reject for the wrong reason.
    # Build the engine's fastembed-backed default once and thread it through
    # (mirroring consolidate_candidates.py). fastembed is the engine's OPTIONAL
    # 'retrieval' extra (NOT installed in the maintenance env); if it's absent,
    # degrade gracefully + log rather than crash — fail-open, consistent with the
    # rest of the pipeline (the dry-run-first gate then surfaces a thin run).
    from ultra_memory import retrieval_core
    try:
        embedder = retrieval_core.default_embedder()
    except Exception as exc:  # fastembed absent → degrade (no memory-side recall)
        print(f"sp7-aggressive: no embedder ({exc}); memory-side recall degraded "
              f"(fail-open) — quarantine/eval will see no near-pairs this run.")
        embedder = None

    rr = run_aggressive_pass(
        conn, repo_root=args.repo_root, date=date, ts=ts, probes=probes,
        embedder=embedder, runner=None, briefings_dir=args.briefings_dir,
        export_fn=_default_export_fn(conn, args.repo_root, ts), log=_log)
    print(f"sp7-aggressive: mode={rr.mode} halt={rr.halt} "
          f"applied={rr.applied_counts} digest={rr.digest_path}")
    return 0                                     # always 0 (fail-open)


if __name__ == "__main__":
    raise SystemExit(main())
