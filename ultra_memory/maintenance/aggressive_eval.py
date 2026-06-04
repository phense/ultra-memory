"""SP-7 §6 — the EVAL GATE (hard + quality, shadow-apply) — Stage 4 of the SP-7
build (spec §7 step 4). The pre-commit defense: the loop must KNOW an aggressive
edit is a net improvement BEFORE committing it.

This module mirrors `scripts/judge_eval_backfill.py`'s ship-gate exactly
(`run_eval` → confusion-matrix → a HARD negative gate `gate_neg_pass=(fp==0)` +
a quality gate → `ship_pass`). The SP-7 analog has TWO gates, both of which the
run must clear to live-apply (else dry-run / skip):

  (1) HARD GATE (ZERO TOLERANCE — the §4a stop-the-world, the fp==0 analog).
      If ANY proposed action targets a forbidden (human / import / pinned) unit →
      gate_hard_pass=False → HALT THE WHOLE RUN + alert. A single forbidden-target
      attempt is NOT a per-item skip — it stops everything (a loop trying to edit a
      human/pinned rule is a bug or a prompt-injection, not routine). The check
      funnels through aggressive_wall.assert_mutable, which RE-READS THE LIVE ROW
      (never trusts an LLM-echoed `created_by`/`pinned` field). When the hard gate
      fails, NOTHING is admitted — not even the legal actions in the same plan.

  (2) QUALITY GATE (the improvement proof — DETERMINISTIC, NO LLM). Before
      admitting an edit, shadow-apply it to a TEMP-DB CLONE of the source store
      (NEVER the live store) and run a FROZEN retrieval probe set
      (queries-with-expected-top-hits) via the engine `unified_recall` against
      (a) the clone WITH the edit applied vs (b) the as-is clone (baseline). The
      edit passes ONLY IF it does NOT regress the probe set (top-k overlap +
      rank-correlation ≥ θ vs the baseline — the SP-5 parity-fence shape, reused).
      An edit that DEGRADES known-good retrieval is REJECTED — kept as a PROPOSED
      action in the digest, NOT applied. No live store ever sees an un-evaluated
      edit. STRICT θ (resolved fork D): reject ANY measurable probe regression —
      "first, do no harm" for an autonomous aggressive edit; a rejected edit is not
      lost (it is in the digest for the operator), so strictness costs little.

THE WALL LIVES IN THE APPLY PATH (code), NEVER ONLY THE PROMPT (spec §4 design
rule + the [[feedback-subagents-can-leak-secrets]] lesson: build the constraint
into the TOOL). The LLM *proposes* the plan; THIS module *enforces* the gates.

DETERMINISTIC + OAUTH-CLEAN: the quality gate is a pure retrieval comparison over
the shadow clone (reusing `unified_recall`, embedder=None → BM25-only, fully
reproducible). There is NO LLM call here — the only LLM in the aggressive pass is
the §5.5 reflection/adjudication that PRODUCES the plan, upstream of this gate.
This keeps the eval from re-paying the token cost ([[feedback-workflow-token-cost]]).
A guard test asserts no anthropic SDK + no OAuth-CLI import.

SHADOW-NEVER-MUTATES-LIVE: the quality gate clones the source db to a throwaway
temp file and shadow-applies edits to the CLONE; the source store is byte-unchanged
after the eval. Only the (separate) Stage-5 apply step — gated on this report's
admitted set, inside the §4d git checkpoint — ever touches the live store.

FAIL-OPEN + fail-CLOSED-to-safety (project rule + spec §4f): an error anywhere in
the quality gate degrades to a SAFE REJECT (an un-evaluable edit is treated as
NOT-passing — do no harm), never raises out into the maintenance run. Note this is
fail-open in the "never wedge the run" sense AND fail-closed in the "an error can
only ever make the pass apply LESS, never proceed unbounded" sense.

The engine primitives it consumes (`unified_recall`, `open_memory_db`,
`save_memory`, `consolidate`) are GENERIC + already on live master (ffcd414). The
probe-set shape, the strict-θ policy, and the action-record schema are the
consumer's policy (e.g. a trading project).
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

# The wall — the single provenance chokepoint (sibling SP-7 module). The hard gate
# funnels every target through assert_mutable, which re-reads the live row.
from ultra_memory.maintenance.aggressive_wall import (  # noqa: E402
    ForbiddenTargetError,
    MemoryUnit,
    assert_mutable,
)

# The engine — generic, project-agnostic primitives (wiki_lib.py:24 precedent).
from ultra_memory import memory_lib  # noqa: E402
from ultra_memory.unified_query import unified_recall  # noqa: E402

# --------------------------------------------------------------------------- #
# §6 — the strict-θ quality-gate policy (resolved fork D: reject ANY regression).
# --------------------------------------------------------------------------- #

# Top-k for the probe recall (the held-out queries-with-expected-top-hits).
PROBE_TOP_K = 5

# θ_OVERLAP — the minimum top-k Jaccard overlap a per-probe result must keep vs
# the baseline. STRICT (fork D): 1.0 means ANY top-k membership change is a
# regression. We use 1.0 — for an autonomous aggressive edit "do no measurable
# harm" outweighs squeezing marginal gains.
THETA_OVERLAP = 1.0

# θ_RANKCORR — the minimum rank-correlation over the COMMON members vs the
# baseline. STRICT: 1.0 means any reordering of shared hits is a regression.
THETA_RANKCORR = 1.0

# The eval runs as the trusted orchestrator (the maintenance pass is the trusted
# CLI path — agent_topics=None ⇒ all topics, the orchestrator scope in
# unified_recall). caller_class chooses the type wall; 'orchestrator' is the
# full-recall class the trusted maintenance pass uses.
_EVAL_CALLER_CLASS = "orchestrator"


# --------------------------------------------------------------------------- #
# Action-target extraction — which live row(s) an opaque plan action targets.
# --------------------------------------------------------------------------- #

def _action_target_ids(cls: str, action: dict) -> list[str]:
    """The live-row id(s) an action of class `cls` targets — the ids the hard gate
    re-reads via assert_mutable. Class-specific:
        edits       → the OLD id being rewritten (old_id)
        reversions  → the regressed id (+ the prior id if present)
        quarantines → BOTH members of the pair (id_a, id_b)
    A missing/blank id yields no target (the hard gate then has nothing to assert;
    the action is inert — it cannot be applied without a target anyway)."""
    ids: list[str] = []
    if cls == "edits":
        for k in ("old_id",):
            v = action.get(k)
            if v:
                ids.append(str(v))
    elif cls == "reversions":
        for k in ("regressed_id", "prior_id"):
            v = action.get(k)
            if v:
                ids.append(str(v))
    elif cls == "quarantines":
        for k in ("id_a", "id_b"):
            v = action.get(k)
            if v:
                ids.append(str(v))
    return ids


# --------------------------------------------------------------------------- #
# (1) The HARD gate — zero-tolerance provenance check over the WHOLE plan.
# --------------------------------------------------------------------------- #

def hard_gate(conn, plan: dict) -> dict:
    """The §4a stop-the-world / fp==0 analog. Inspect EVERY proposed action across
    every class; re-read each target's LIVE row via assert_mutable. A SINGLE
    forbidden (human/import/pinned/unprovable) target → gate_hard_pass=False +
    halt + the offending targets recorded (for the alert + the digest). This is
    NOT a per-item skip — one violation halts the whole run.

    Returns {gate_hard_pass: bool, forbidden_targets: [str]}. Fail-CLOSED: a
    malformed action class is skipped for target extraction but never silently
    passes a forbidden row (an unparseable plan yields no targets → the quality
    gate then admits nothing, the safe outcome)."""
    forbidden: list[str] = []
    if not isinstance(plan, dict):
        return {"gate_hard_pass": True, "forbidden_targets": []}
    for cls, actions in plan.items():
        if not isinstance(actions, list):
            # A malformed class contributes no targets (and the quality gate will
            # admit nothing from it) — it does not make a forbidden row pass.
            continue
        for action in actions:
            if not isinstance(action, dict):
                continue
            for tid in _action_target_ids(cls, action):
                try:
                    assert_mutable(conn, MemoryUnit(tid))
                except ForbiddenTargetError as exc:
                    forbidden.append(f"{tid}: {exc}")
                except Exception as exc:  # any unexpected error → fail-closed forbid
                    forbidden.append(f"{tid}: unexpected gate error ({exc!r})")
    return {"gate_hard_pass": not forbidden, "forbidden_targets": forbidden}


# --------------------------------------------------------------------------- #
# Shadow store — a throwaway temp-DB clone the quality gate edits, never the live.
# --------------------------------------------------------------------------- #

def _source_db_path(conn) -> str:
    """The on-disk path backing an open memory connection (the clone source)."""
    for row in conn.execute("PRAGMA database_list").fetchall():
        # row: (seq, name, file)
        if row[1] == "main":
            return row[2]
    raise RuntimeError("clone_store: no 'main' database path on the connection")


def clone_store(source_path: str, tmp_dir) -> str:
    """Produce a SEPARATE on-disk db clone of `source_path` under `tmp_dir`, using
    the sqlite backup API so the snapshot is consistent even with WAL pending. The
    clone is a throwaway the quality gate shadow-mutates; the source is never
    touched. Returns the clone path.

    (We open the source READ-ONLY for the backup so the clone cannot accidentally
    write back — the shadow lives entirely in the new file.)"""
    tmp_dir = Path(tmp_dir)
    clone_path = tmp_dir / f"sp7-shadow-{Path(source_path).name}"
    src = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(clone_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return str(clone_path)


def _shadow_apply_edit(clone_conn, action: dict, ts: str) -> None:
    """Shadow-apply ONE proposed auto-edit to the CLONE in a way that mirrors the
    live apply's recall effect: write the new body onto a fresh active row and
    redirect the old one (the real §5.1 apply uses save_memory+consolidate; the
    shadow only needs the recall-visible end state — the new active body, the old
    out of recall). We reuse the engine primitives on the CLONE so the shadow's
    recall reflects exactly what the live apply would produce.

    This runs against the THROWAWAY clone connection only — never the live store."""
    old_id = str(action["old_id"])
    new_body = action.get("new_body", "")
    new_title = action.get("new_title", "")
    # Preserve the OLD row's `type` on the new version so the shadow recall is
    # apples-to-apples (the live §5.1 apply preserves type; a type swap would make
    # the new row invisible to a type-scoped recall and OVER-detect a regression).
    old_row = clone_conn.execute(
        "SELECT type, title FROM memories WHERE id=?", (old_id,)).fetchone()
    new_type = old_row["type"] if old_row is not None else "reference"
    if not new_title and old_row is not None:
        new_title = old_row["title"]
    # Mirror the live recall end-state on the clone: redirect the old row out of
    # recall and write the sharpened version as an active agent-provenance row.
    new_id = _shadow_new_id(old_id)
    memory_lib.save_memory(
        clone_conn, id=new_id, type=new_type, title=new_title, body=new_body,
        ts=ts, created_by="background_review")
    memory_lib.consolidate(
        clone_conn, loser_id=old_id, canonical_id=new_id,
        reason="sp7-shadow-eval", ts=ts)


def _shadow_new_id(old_id: str) -> str:
    """The deterministic shadow id for an edited unit's new version. The metric
    normalizes this BACK to `old_id` when comparing (the edit is the SAME logical
    unit — a clean edit must not look like a regression just because the versioned
    row id changed)."""
    return f"shadow-edit-{old_id}"


# --------------------------------------------------------------------------- #
# The frozen-probe metric — top-k overlap + rank correlation (strict θ).
# --------------------------------------------------------------------------- #

def _probe_recall_ids(conn, query: str, embedder) -> list[str]:
    """Run one probe query via unified_recall (orchestrator scope, all topics) and
    return the ranked hit keys best-first. A memory hit keys on `id`; a knowledge
    hit on `slug` — both are stable identifiers for the overlap/rank comparison.

    NOTE the MEMORY-ONLY byte-identity path (unified_recall §5.6 invariant): when
    no knowledge row is in scope, unified_recall returns the RAW query_memories
    dicts, which carry `id` but NO `source_kind` key. So a hit is a memory hit iff
    it is tagged `source_kind='memory'` OR (untagged but) carries an `id` — we key
    on `slug` only for an explicit `source_kind='knowledge'` hit."""
    hits = unified_recall(
        conn, query, caller_class=_EVAL_CALLER_CLASS, agent_topics=None,
        embedder=embedder, top_k=PROBE_TOP_K, audit=False)
    keys: list[str] = []
    for h in hits:
        if h.get("source_kind") == "knowledge":
            keys.append(h.get("slug"))
        else:
            # memory hit (explicit source_kind='memory' OR the byte-identity path's
            # raw query_memories dict, which has `id` but no source_kind).
            keys.append(h.get("id"))
    return [k for k in keys if k is not None]


def _spearman_like(common_order_base: list, common_order_after: list) -> float:
    """A rank-correlation over the COMMON members: 1.0 iff the shared hits keep the
    same relative order, degrading toward -1.0 as they reverse. We use a simple
    normalized rank-difference (a Spearman-footprint, no scipy dependency):
        corr = 1 - 2 * inversions / max_inversions
    Empty / single common member → 1.0 (no order to disagree on)."""
    n = len(common_order_base)
    if n < 2:
        return 1.0
    pos_after = {k: i for i, k in enumerate(common_order_after)}
    seq = [pos_after[k] for k in common_order_base]
    inversions = 0
    for i in range(n):
        for j in range(i + 1, n):
            if seq[i] > seq[j]:
                inversions += 1
    max_inv = n * (n - 1) // 2
    if max_inv == 0:
        return 1.0
    return 1.0 - 2.0 * inversions / max_inv


def regresses(baseline_ranked: list, after_ranked: list,
              *, theta_overlap: float = THETA_OVERLAP,
              theta_rankcorr: float = THETA_RANKCORR) -> bool:
    """Does `after_ranked` REGRESS vs `baseline_ranked` for one probe? STRICT θ
    (fork D): a regression iff EITHER
      • the top-k Jaccard overlap of members drops below theta_overlap
        (any baseline top-k hit dropping out of the after top-k), OR
      • the rank-correlation over the COMMON members drops below theta_rankcorr
        (any reordering of the shared hits).
    Identical lists → no regression. Fail-CLOSED elsewhere: the caller treats an
    UN-MEASURABLE edit (an error) as a regression (a safe reject)."""
    base = list(baseline_ranked)
    after = list(after_ranked)
    base_set = set(base)
    after_set = set(after)
    if not base_set:
        # No baseline signal for this probe → nothing to regress against.
        return False
    inter = base_set & after_set
    union = base_set | after_set
    overlap = len(inter) / len(union) if union else 1.0
    if overlap < theta_overlap:
        return True
    # Rank-correlation over the common members (in each list's order).
    common_base = [k for k in base if k in inter]
    common_after = [k for k in after if k in inter]
    if _spearman_like(common_base, common_after) < theta_rankcorr:
        return True
    return False


def expected_hit_regressed(expected, baseline_ranked: list,
                           after_ranked: list) -> bool:
    """The PROBE-ANCHORED regression test (spec §6: a frozen fixture of
    'queries-with-expected-top-hits'). The load-bearing signal for a probe is its
    EXPECTED hit; a regression iff the edit DEMOTES that expected hit:
      • it was retrieved in the baseline top-k but DROPPED OUT of the after top-k, OR
      • it is RANKED WORSE (a higher index) in the after list than the baseline.
    A clean edit that only perturbs the INCIDENTAL tail ordering of unrelated docs
    (which any legitimate added term will do) is NOT a regression — only a demotion
    of the known-good target is. STRICT (fork D): ANY demotion of the expected hit
    rejects; an improvement (promotion) or an unchanged rank passes.

    If the probe declares no `expected` hit, falls back to the generic `regresses`
    (the whole-list strict metric) so an un-anchored probe is still guarded."""
    if expected is None:
        return regresses(baseline_ranked, after_ranked)
    base_rank = baseline_ranked.index(expected) if expected in baseline_ranked else None
    after_rank = after_ranked.index(expected) if expected in after_ranked else None
    if base_rank is None:
        # The expected hit was not even retrieved at baseline → no baseline signal
        # to regress against for this probe (the fixture is stale for it).
        return False
    if after_rank is None:
        return True                      # dropped out of the top-k → regression
    return after_rank > base_rank        # demoted (worse rank) → regression


def _score_edit_on_shadow(source_path: str, action: dict, probes: list, *,
                          tmp_dir, embedder, ts: str) -> bool:
    """The quality-gate verdict for ONE edit: True iff it does NOT regress the
    frozen probe set on a shadow clone. Two shadow clones are scored:
      (a) the as-is clone (the baseline ranked lists), and
      (b) a clone with the edit shadow-applied (the after ranked lists);
    the edit regresses iff ANY probe regresses (strict θ).

    Each clone is an independent throwaway file — the SOURCE store is never
    touched. Raises on an internal error (the caller's fail-open turns a raise into
    a SAFE REJECT — do no harm)."""
    # (a) baseline ranked lists over the as-is clone.
    base_path = clone_store(source_path, tmp_dir)
    base_conn = memory_lib.open_memory_db(base_path)
    try:
        baselines = {p_i: _probe_recall_ids(base_conn, probes[p_i]["query"], embedder)
                     for p_i in range(len(probes))}
    finally:
        base_conn.close()

    # (b) after ranked lists over a clone with the edit shadow-applied. The
    # edited unit's NEW version carries a versioned shadow id; we normalize it
    # BACK to the original old_id so the metric treats the edit as the SAME
    # logical unit (a clean edit must not regress merely because the row id
    # changed — only a CONTENT degradation that drops the unit's RANK counts).
    old_id = str(action["old_id"])
    remap = {_shadow_new_id(old_id): old_id}
    after_path = clone_store(source_path, tmp_dir)
    after_conn = memory_lib.open_memory_db(after_path)
    try:
        _shadow_apply_edit(after_conn, action, ts)
        for p_i in range(len(probes)):
            raw = _probe_recall_ids(after_conn, probes[p_i]["query"], embedder)
            after_ranked = [remap.get(k, k) for k in raw]
            expected = probes[p_i].get("expect")
            if expected_hit_regressed(expected, baselines[p_i], after_ranked):
                return False   # a regression on any probe's target → reject
    finally:
        after_conn.close()
    return True


# --------------------------------------------------------------------------- #
# (2) The QUALITY gate over the edit class + the run report.
# --------------------------------------------------------------------------- #

def run_aggressive_eval(conn, plan: dict, *, probes: list, embedder=None,
                        tmp_dir=None, ts: str | None = None,
                        apply: bool = False) -> dict:
    """The SP-7 ship-gate (spec §6), mirroring judge_eval_backfill.run_eval.

    1. HARD GATE (zero tolerance): if ANY proposed action targets a forbidden
       (human/import/pinned) unit → gate_hard_pass=False → HALT THE WHOLE RUN.
       NOTHING is admitted when the hard gate fails (not even the legal actions).
    2. QUALITY GATE (deterministic shadow-apply): for each proposed EDIT, score it
       on a throwaway clone against the frozen probe set; ADMIT only non-regressing
       edits; keep regressing edits as PROPOSED-but-REJECTED (digested, not lost).
       Reversions + quarantines are NOT retrieval-quality-evaluable the same way
       (a reversion/quarantine REMOVES a unit from recall by design); they pass the
       hard gate and are admitted as-is for the (separately-bounded) apply step.

    FIX 4 (fail-CLOSED on empty probes): the strict quality gate's ONLY reject
    signal is a per-probe regression. With NO probes the gate would silently
    return True (admit) for EVERY edit — defeating the documented regression gate
    on the LIVE aggressive run (run_maintenance.sh passes no --probes). When
    `apply=True` (LIVE) AND the probe set is empty, we REFUSE to admit any edit —
    every edit is HELD (routed to `rejected`, flagged `no_probe_coverage`) so an
    un-evaluable edit is never live-applied unchecked. A DRY-RUN (`apply=False`)
    with empty probes may still compute the digest (it applies nothing anyway), but
    it does NOT mark edits admittable-in-live (also held, same flag) so the digest
    is honest about the missing coverage. The fast-path is unchanged when probes
    is non-empty.

    Returns a report dict:
        {gate_hard_pass, halt, forbidden_targets, no_probe_coverage, notes,
         admitted: [action, ...],        # cleared both gates → apply-eligible
         rejected: [action, ...],        # cleared hard, failed quality → digest
         reversions: [...], quarantines: [...]}   # hard-passing non-edit classes

    FAIL-OPEN: never raises out — a malformed plan / a scoring error degrades to a
    SAFE outcome (the offending edit is rejected; the run still returns a report)."""
    ts = ts or "1970-01-01T00:00:00Z"
    report = {
        "gate_hard_pass": True,
        "halt": False,
        "forbidden_targets": [],
        "admitted": [],
        "rejected": [],
        "reversions": [],
        "quarantines": [],
        "no_probe_coverage": False,
        "notes": [],
    }

    # --- (1) HARD GATE — zero tolerance, stop-the-world ---------------------- #
    hg = hard_gate(conn, plan)
    report["gate_hard_pass"] = hg["gate_hard_pass"]
    report["forbidden_targets"] = hg["forbidden_targets"]
    if not hg["gate_hard_pass"]:
        # The §4a stop-the-world: halt, admit NOTHING (not even the legal actions).
        report["halt"] = True
        return report

    if not isinstance(plan, dict):
        return report

    # --- FIX 4: empty-probe FAIL-CLOSED — no coverage ⇒ admit no edit -------- #
    # An empty probe set gives the strict quality gate zero ability to detect a
    # regression. Rather than silently admit every edit (the bug), HOLD them all:
    # route every (hard-passing) edit to `rejected` flagged no_probe_coverage. The
    # non-edit classes (reversions/quarantines) are NOT retrieval-evaluable, so
    # they still pass the hard gate as before — but a LIVE run with no probes must
    # not auto-apply un-evaluable EDITS. Applies in both live and dry-run so the
    # digest never reports a no-coverage edit as admittable-in-live.
    if not probes:
        edits = plan.get("edits", [])
        edits = edits if isinstance(edits, list) else []
        report["rejected"].extend(e if isinstance(e, dict) else {} for e in edits)
        report["no_probe_coverage"] = True
        report["notes"].append(
            "no probe set → 0 edits admitted (provide --probes to enable the "
            "strict quality gate; "
            + ("LIVE" if apply else "dry-run")
            + " held all edits for no-coverage)")
        revs = plan.get("reversions", [])
        report["reversions"] = list(revs) if isinstance(revs, list) else []
        quars = plan.get("quarantines", [])
        report["quarantines"] = list(quars) if isinstance(quars, list) else []
        return report

    # --- (2) QUALITY GATE — deterministic shadow-apply over the edit class --- #
    # The clone source is the live connection's backing file; the gate clones it.
    try:
        source_path = _source_db_path(conn)
    except Exception:
        source_path = None

    # A managed temp dir if the caller did not supply one (so the gate stays
    # self-contained); the clones are throwaways under it.
    import tempfile
    own_tmp = None
    if tmp_dir is None:
        own_tmp = tempfile.TemporaryDirectory(prefix="sp7-eval-")
        tmp_dir = own_tmp.name

    try:
        edits = plan.get("edits", [])
        edits = edits if isinstance(edits, list) else []
        for action in edits:
            if not isinstance(action, dict) or not action.get("old_id"):
                # A malformed edit cannot be scored → SAFE reject.
                report["rejected"].append(action if isinstance(action, dict) else {})
                continue
            try:
                if source_path is None:
                    raise RuntimeError("no clonable source path")
                passed = _score_edit_on_shadow(
                    source_path, action, probes, tmp_dir=tmp_dir,
                    embedder=embedder, ts=ts)
            except Exception:
                # FAIL-OPEN → fail-CLOSED-to-safety: an un-evaluable edit is a SAFE
                # REJECT (do no harm), never admitted, never raised out.
                passed = False
            (report["admitted"] if passed else report["rejected"]).append(action)

        # Non-edit classes cleared the hard gate; they are admitted for the
        # (separately-bounded §4c) apply step. They are not retrieval-quality
        # evaluable the same way (they REMOVE a unit from recall by design).
        revs = plan.get("reversions", [])
        report["reversions"] = list(revs) if isinstance(revs, list) else []
        quars = plan.get("quarantines", [])
        report["quarantines"] = list(quars) if isinstance(quars, list) else []
    finally:
        if own_tmp is not None:
            own_tmp.cleanup()

    return report
