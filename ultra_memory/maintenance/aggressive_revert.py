"""SP-7 §5.2 — the OUTCOME-BASED SELF-REVERSION track — Stage 6 of the SP-7 build
(spec §7 step 6), under the RESOLVED fork A: reversion is PROPOSE-FOR-PETER (NOT
fully autonomous). The SECOND of the three aggressive self-improvement
capabilities, built ON TOP of the safety wall (Stages 1-4) + the deterministic
outcome signal (Stage 3, `aggressive_outcomes`). It re-implements no guard — it
COMPOSES the wall (`aggressive_wall.apply_revert`, the FSM-flip apply path), the
hard gate (`aggressive_eval.hard_gate`, zero-tolerance provenance), the bounds
(`aggressive_bounds.MAX_REVERSIONS_PER_RUN`), and the regression signal
(`aggressive_outcomes.detect_regressions` + `is_regression`).

THE GEPA/PARETO POST-COMMIT DEFENSE (spec §5.2): the eval gate (§6) is the
*pre-commit* defense (block a degrading auto-edit before it lands); self-reversion
is the *post-commit* defense (revert an edit that PASSED the gate but degraded in
the wild). A graduated/auto-edited unit whose linked downstream `outcome_signal`s
REGRESSED (net-negative AND below the pre-edit baseline, past the MIN_EVIDENCE
noise floor) is reverted to its prior version — or, with no prior, demoted to
quarantined (out of recall) rather than reverted to nothing.

FORK A — PROPOSE-FOR-PETER (the resolved default, spec §5 banner / §10 fork A):
  Reverting is the action MOST LIKELY to be ITSELF WRONG (a regression may be
  noise, not the lesson's fault — Risk §9.3). So unlike auto-edit + quarantine
  (which run autonomously within the wall), the self-reversion track's DEFAULT is
  to PROPOSE the reversion TO THE DIGEST — the regressed unit, its prior version,
  the regression evidence — WITHOUT applying it. The operator confirms. The reversion
  MECHANISM is still BUILT here (so an operator-confirmed reversion can execute): the
  `apply=True` path runs it through the same hard-gate + bound + wall as every
  aggressive verb. The mechanism is a pure FSM flip (regressed→'reverted',
  prior→'active') — archive-never-delete, fully reversible. A no-prior graduation
  demotes to 'quarantined' instead of reverting to nothing.

THE WALL LIVES IN THE APPLY PATH (code), NEVER ONLY THE PROMPT (spec §4 design
rule + the [[feedback-subagents-can-leak-secrets]] lesson: build the constraint
into the TOOL). There is NO LLM in this track at all (the regression signal is
deterministic, the proposal is deterministic) — but the apply path is STILL
gated: `apply_reversions` runs the hard gate over the WHOLE plan first
(assert_mutable RE-READS each target's live row), so a single forbidden target
HALTS the whole apply (the §4a stop-the-world, zero tolerance, NOT a per-item
skip) — and it is bounded (halt-on-exceed).

OAUTH-ONLY (HARD): this module makes NO model call of ANY kind (the §5.2 signal is
deterministic — that is the whole point of the no-LLM regression detector). There
is NO anthropic-SDK import, no API-key read, no `claude` subprocess (a guard test
asserts it). ARCHIVE-NEVER-DELETE: every verb is a reversible FSM transition via
the wall primitive `apply_revert` — NO rm / os.remove / memory_lib.delete anywhere
(a guard test asserts it). FAIL-OPEN: any error in detection / proposal / apply
degrades to an EMPTY result / a no-op — it never raises out into the nightly /
monthly maintenance run (the one exception is a ForbiddenTargetError from the wall
on the confirm path, which is the §4a zero-tolerance stop-the-world the hard gate
is supposed to have caught first; it propagates as the halt).

The engine primitives the wall consumes (set_status / record_link) are GENERIC +
already on live master (ffcd414). The regression definition, the MIN_EVIDENCE
floor, the propose-for-the-operator default, and the MAX_REVERSIONS policy are the
consumer's policy (e.g. a trading project).
"""
from __future__ import annotations

import sys
from pathlib import Path

# The sibling SP-7 modules — the outcome signal (regression detector), the wall
# (the FSM-flip apply path), the hard gate (zero-tolerance provenance), the bounds.
# This track COMPOSES them; it re-implements no guard.
from ultra_memory.maintenance import aggressive_outcomes as ao  # noqa: E402
from ultra_memory.maintenance.aggressive_bounds import MAX_REVERSIONS_PER_RUN  # noqa: E402
from ultra_memory.maintenance.aggressive_eval import hard_gate  # noqa: E402
from ultra_memory.maintenance.aggressive_wall import apply_revert  # noqa: E402

# The engine — generic, project-agnostic primitives (wiki_lib.py:24 precedent).

# Per-run cap (mirrors the bound the apply enforces). Default from aggressive_bounds.
MAX_REVERSIONS = MAX_REVERSIONS_PER_RUN


# --------------------------------------------------------------------------- #
# 0. The regression evidence bundle (the deterministic §5.2 signal, no LLM).
# --------------------------------------------------------------------------- #

def _evidence_for(conn, *, regressed_id, prior_id) -> dict:
    """Bundle the deterministic regression evidence for a proposal — the
    (net score, count) of the regressed unit's linked outcomes + the prior's, so
    the operator can adjudicate the reversion straight from the digest. NO LLM: it reads
    the same `aggressive_outcomes` aggregate the regression test reasons over.
    Fail-soft: a read error yields a minimal evidence dict (zeros), never raises."""
    try:
        reg_net, reg_n = ao._net_and_count(conn, regressed_id)
    except Exception:
        reg_net, reg_n = 0, 0
    prior_net, prior_n = 0, 0
    if prior_id is not None:
        try:
            prior_net, prior_n = ao._net_and_count(conn, prior_id)
        except Exception:
            prior_net, prior_n = 0, 0
    return {
        "regressed_net": reg_net, "regressed_n": reg_n,
        "prior_net": prior_net, "prior_n": prior_n,
        "min_evidence": ao.MIN_EVIDENCE,
    }


# --------------------------------------------------------------------------- #
# 1. Select regressions (no-LLM) — the auto-edit lineage + (optionally) the
#    no-prior graduations.
# --------------------------------------------------------------------------- #

def _no_prior_regressed_graduations(conn) -> list[dict]:
    """Agent-authored ACTIVE units that graduated WITHOUT a prior version (no
    superseded_by lineage pointing INTO them) but whose own linked outcomes
    REGRESSED (net-negative past the floor — `is_regression` with prior_id=None).
    These cannot revert to a prior (there is none), so the §5.2 mechanism DEMOTES
    them to 'quarantined' instead of reverting to nothing. Fail-open-to-empty."""
    try:
        rows = conn.execute(
            "SELECT id FROM memories "
            "WHERE created_by IN ('agent','background_review') "
            "AND status='active' AND pinned=0"
        ).fetchall()
    except Exception:
        return []

    out: list[dict] = []
    for r in rows:
        uid = r["id"]
        try:
            # Skip units that ARE the NEW side of an auto-edit lineage — those are
            # handled by detect_regressions (they HAVE a prior). A no-prior
            # graduation has no superseded_by edge with dst=this unit.
            has_prior = conn.execute(
                "SELECT 1 FROM links WHERE predicate='superseded_by' "
                "AND src_kind='memory' AND dst_kind='memory' AND dst_id=? LIMIT 1",
                (uid,),
            ).fetchone()
            if has_prior is not None:
                continue
            if ao.is_regression(conn, regressed_id=uid, prior_id=None):
                out.append({"regressed_id": uid, "prior_id": None})
        except Exception:
            continue                              # fail-open per unit
    return out


def select_reversions(conn, *, include_graduations: bool = False) -> list[dict]:
    """The no-LLM §5.2 selection. Returns the regression candidates:

      (a) the auto-edit LINEAGE regressions (via `ao.detect_regressions`): a unit
          whose post-edit outcomes are net-negative AND below its prior version's
          baseline — each {regressed_id, prior_id} (prior_id is the superseded old
          version, the revert target);
      (b) (optional, `include_graduations`) the NO-PRIOR regressed graduations: a
          freshly-graduated lesson that only produced losses — each
          {regressed_id, prior_id=None} (the demote-to-quarantined case).

    Fail-open-to-empty on any error. NEVER selects a human / pinned unit (the
    underlying scans restrict to created_by IN ('agent','background_review'))."""
    out: list[dict] = []
    seen: set = set()
    try:
        for reg in ao.detect_regressions(conn):
            rid = reg.get("regressed_id")
            if rid and rid not in seen:
                out.append({"regressed_id": rid, "prior_id": reg.get("prior_id")})
                seen.add(rid)
    except Exception:
        pass                                      # fail-open
    if include_graduations:
        try:
            for grad in _no_prior_regressed_graduations(conn):
                rid = grad.get("regressed_id")
                if rid and rid not in seen:
                    out.append({"regressed_id": rid, "prior_id": None})
                    seen.add(rid)
        except Exception:
            pass                                  # fail-open
    return out


# --------------------------------------------------------------------------- #
# 2. Build proposals (the propose-for-the-operator digest payload).
# --------------------------------------------------------------------------- #

def build_proposals(conn, candidates: list[dict]) -> list[dict]:
    """Turn the selected regression candidates into PROPOSALS for the digest — the
    propose-for-the-operator payload (fork A). Each proposal carries the regressed unit,
    its prior version (or None), the deterministic regression EVIDENCE, and the
    `action` the confirm path would take: 'revert' (a prior exists → FSM flip) or
    'demote' (no prior → quarantine). Fail-open per candidate."""
    proposals: list[dict] = []
    for c in candidates:
        try:
            rid = c.get("regressed_id")
            if not rid:
                continue
            prior_id = c.get("prior_id")
            proposals.append({
                "regressed_id": rid,
                "prior_id": prior_id,
                "action": "revert" if prior_id is not None else "demote",
                "evidence": _evidence_for(conn, regressed_id=rid, prior_id=prior_id),
            })
        except Exception:
            continue                              # fail-open per candidate
    return proposals


# --------------------------------------------------------------------------- #
# 3. Apply — the reversion MECHANISM (the operator-confirm path), gated + bounded.
# --------------------------------------------------------------------------- #

def apply_reversions(conn, reversions: list, *, ts: str,
                     max_reversions: int = MAX_REVERSIONS) -> list[dict]:
    """Apply the confirmed reversions via the wall's `apply_revert` — a pure FSM
    flip (regressed→'reverted', prior→'active') or, for a no-prior unit, a demote
    to 'quarantined'. Each call funnels through the wall's assert_mutable (RE-READS
    the live row) — provenance is enforced in the apply path, never a prompt.

    BOUNDED to `max_reversions`, HALT-ON-EXCEED (§4c): a plan LARGER than the cap
    applies NONE of the class (not the first N) — a volume far over the cap is a
    signal something is wrong, so stop-and-ask. Returns the list of applied
    reversions (each {regressed_id, prior_id}) — empty when the bound halts.

    A ForbiddenTargetError from the wall PROPAGATES (the §4a zero-tolerance stop-
    the-world): a PRE-FLIGHT hard gate re-reads EVERY target's live row BEFORE any
    write, so a single forbidden target halts the batch with NOTHING applied (not
    even the legal reversions earlier in the list) — the true stop-the-world, not a
    per-item skip."""
    reversions = reversions if isinstance(reversions, list) else []
    # HALT-ON-EXCEED: do not even start if the batch is over the cap.
    if len(reversions) > max_reversions:
        return []

    # Normalize the batch to the well-formed reversions we will actually apply.
    batch: list[dict] = []
    for rev in reversions:
        if not isinstance(rev, dict) or not rev.get("regressed_id"):
            continue
        batch.append({
            "regressed_id": str(rev["regressed_id"]),
            "prior_id": (str(rev["prior_id"]) if rev.get("prior_id") is not None
                         else None),
        })

    # PRE-FLIGHT provenance check over the WHOLE batch (the §4a stop-the-world):
    # the hard gate re-reads each target's live row; a single forbidden target
    # makes it propagate a ForbiddenTargetError HERE, before any write — so NOTHING
    # in the batch is applied. (We funnel through the SAME hard_gate the eval uses,
    # then raise on its forbidden_targets, so the wall's assert_mutable is the one
    # authority and a test can catch ForbiddenTargetError just like the edit track.)
    _assert_batch_mutable(conn, batch)

    # All targets cleared the wall → apply the FSM-flip / demote reversions.
    applied: list[dict] = []
    for rev in batch:
        apply_revert(conn, regressed_id=rev["regressed_id"],
                     prior_id=rev["prior_id"], ts=ts)
        applied.append({"regressed_id": rev["regressed_id"],
                        "prior_id": rev["prior_id"]})
    return applied


def _assert_batch_mutable(conn, batch: list[dict]) -> None:
    """PRE-FLIGHT the whole batch through the hard gate (zero-tolerance provenance).
    The hard gate re-reads each reversion target's LIVE row via assert_mutable; a
    single forbidden target makes it raise a ForbiddenTargetError HERE (the §4a
    stop-the-world), before any write. We re-raise the wall's own exception so the
    apply path's authority is the wall (never an LLM-echoed field)."""
    from ultra_memory.maintenance.aggressive_wall import ForbiddenTargetError, MemoryUnit, assert_mutable
    plan = {"reversions": [
        {"regressed_id": r["regressed_id"], "prior_id": r["prior_id"]}
        for r in batch
    ]}
    report = hard_gate(conn, plan)
    if report["gate_hard_pass"]:
        return
    # Re-derive the FIRST forbidden target as the wall's own exception so the caller
    # (and the test) catches a ForbiddenTargetError — the same contract the edit
    # track's apply_edits uses (it lets assert_mutable raise directly).
    for rev in batch:
        for tid in (rev["regressed_id"], rev["prior_id"]):
            if tid is None:
                continue
            try:
                assert_mutable(conn, MemoryUnit(tid))
            except ForbiddenTargetError:
                raise
    # Defensive: the hard gate failed but no per-target raise reproduced it
    # (e.g. an unexpected gate error). Fail-closed: raise the stop-the-world.
    raise ForbiddenTargetError(
        f"reversion batch failed the hard gate: {report['forbidden_targets']}")


# --------------------------------------------------------------------------- #
# The track entry — select → propose (default) → confirm-apply (opt-in).
# --------------------------------------------------------------------------- #

def run_revert_track(conn, *, ts: str, apply: bool = False,
                     include_graduations: bool = False,
                     max_reversions: int = MAX_REVERSIONS) -> dict:
    """Run the self-reversion track (spec §5.2, fork A).

    1. SELECT regressions (no-LLM): the auto-edit lineage regressions + (optional)
       the no-prior regressed graduations.
    2. PROPOSE (always): build the propose-for-the-operator payload — each {regressed_id,
       prior_id, action, evidence}. This is the digest content the operator adjudicates.
    3. APPLY (ONLY if `apply` is True — the operator-CONFIRM path): run the confirmed
       reversions through the hard gate + the bound + the wall (`apply_reversions`).
       The DEFAULT (`apply=False`) is the propose-for-the-operator path: it PLANS +
       PROPOSES but applies NOTHING — the operator confirms before any reversion lands.

    Returns {proposed, applied, halt, forbidden_targets}. FAIL-OPEN: any error in
    selection / proposal degrades to an EMPTY result; never raises out into the
    maintenance run. (A ForbiddenTargetError on the confirm apply propagates as the
    §4a zero-tolerance stop-the-world — the orchestrator turns it into a run halt.)"""
    result = {"proposed": [], "applied": [], "halt": False,
              "forbidden_targets": []}
    try:
        candidates = select_reversions(
            conn, include_graduations=include_graduations)
        result["proposed"] = build_proposals(conn, candidates)
    except Exception:
        return result                             # fail-open: empty proposal

    # The propose-for-the-operator DEFAULT: apply NOTHING. The digest carries `proposed`;
    # the operator confirms, and only then is this called with apply=True.
    if not apply:
        return result

    # The operator-CONFIRM path: apply the proposed reversions through the wall + bound.
    # (A ForbiddenTargetError here is the §4a stop-the-world; it propagates — the
    #  hard gate should have caught it, so reaching it means a bug/injection.)
    reversions = [
        {"regressed_id": p["regressed_id"], "prior_id": p["prior_id"]}
        for p in result["proposed"]
    ]
    result["applied"] = apply_reversions(
        conn, reversions, ts=ts, max_reversions=max_reversions)
    return result
