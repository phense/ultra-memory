"""SP-7 §5.2 — the deterministic, NO-LLM OUTCOME-WEIGHT EWMA AGGREGATE + the
REGRESSION SIGNAL (the GEPA/Pareto core's *signal* layer).

This is Stage 3 of the SP-7 build (spec §7 step 3). It folds, for each
agent-authored unit, the `outcome_signal`s of its linked `session_events` into a
recency-weighted EWMA and writes the result back to `memories.outcome_weight`
through the engine `set_outcome_weight` — the FIRST non-1.0 writer of that 0004
column (until now it was read-only / inert at its 1.0 default in unified ranking).
A sub-1.0 weight demotes a unit's recall rank; a >1.0 weight promotes it.

It is DETERMINISTIC — there is NO LLM call here (the §5.5 reflection call lives in
the auto-edit / quarantine tracks, not in the signal). SP-3 D13's "deterministic
capture → periodic aggregate": the per-event `outcome_signal` is captured on the
warm path with no LLM; THIS module is the periodic aggregate over those captures.
A guard test asserts no anthropic SDK + no OAuth-CLI import (this module makes no
model call of any kind).

SCORING (spec §5.2):
    +1  {tests_passed, backtest_improved, trade_win, commit_landed}
    -1  {tests_failed, backtest_regressed, trade_loss, reverted}
     0  anything else / a NULL signal (never silently a win or a loss).

THE OUTCOME GRAPH (the reverse-edge idx_links_dst scan): SP-6 writes the
`validated_as` edge as src=session_event -> dst=memory; SP-7's auto-edit writes
`superseded_by` (old -> new) and an event-evidence `superseded_by` (event -> new).
So a unit's outcomes are found on the DST side:
    SELECT src_id FROM links
     WHERE dst_kind='memory' AND dst_id=<unit>
       AND predicate IN ('validated_as','superseded_by')
       AND src_kind='session_event'
then read each `session_events.outcome_signal`. The dst-side index `idx_links_dst`
(migration 0004) makes this a point lookup, not a full scan.

REGRESSION (the §5.2 strict definition — the trigger the Stage-6 reversion track
consumes): an edit/graduation whose post-edit linked outcomes are net-NEGATIVE AND
below the pre-edit baseline (the unit made things WORSE than before the loop
touched it). A MIN_EVIDENCE floor (>=10, fork B "conservative") ensures a single
noisy bad outcome is NOT a regression — auto-reverting on noise would DESTROY good
lessons (Risk §9.3: "a regression from a handful of outcomes may be luck").

FAIL-OPEN (project rule + spec §4f): any aggregate error degrades to a per-unit
no-op (the unit keeps its prior weight) — it never raises out into the nightly /
monthly maintenance run. ARCHIVE-NEVER-DELETE: this module only WRITES
outcome_weight + READS edges; it never deletes (a guard test asserts it).

The engine primitives it consumes (`set_outcome_weight`, `record_session_event`'s
`outcome_signal` column, the `links` reverse edge) are GENERIC + already on live
master (ffcd414). The +1/-1 policy, the EWMA shape, the MIN_EVIDENCE floor, and
the regression definition are the CONSUMER's (Trading-side) POLICY.
"""
from __future__ import annotations

import sys

# The engine — generic, project-agnostic primitives (wiki_lib.py:24 precedent).
from ultra_memory.memory_lib import set_outcome_weight  # noqa: E402

# --------------------------------------------------------------------------- #
# §5.2 scoring policy (the +1/-1 table) + the conservative MIN_EVIDENCE floor.
# --------------------------------------------------------------------------- #

POSITIVE_SIGNALS = frozenset(
    {"tests_passed", "backtest_improved", "trade_win", "commit_landed"})
NEGATIVE_SIGNALS = frozenset(
    {"tests_failed", "backtest_regressed", "trade_loss", "reverted"})

# Fork B "conservative" (spec §5.2 + §10 B): a unit needs >= MIN_EVIDENCE linked
# outcomes before the loop will treat a net-negative aggregate as a REGRESSION. A
# small handful of bad outcomes is noise, not signal — the floor is the structural
# defense against auto-reverting a good lesson on luck.
MIN_EVIDENCE = 10

# EWMA smoothing factor (recency weight). Higher α = recent outcomes dominate more.
# 0.30 gives recent outcomes clear primacy while still reflecting history — the
# recency-weighted aggregate the spec calls for ("e.g. an EWMA").
EWMA_ALPHA = 0.30

# The outcome edges that carry a unit's downstream outcomes (spec §5.2). An edge of
# any OTHER predicate (e.g. 'contradicts') is NOT an outcome edge.
#
#   validated_as / superseded_by  — the loop's OWN bookkeeping edges: the single
#       graduation edge (consolidate_candidates.py) + the auto-edit lineage. They are
#       the loop reasoning about its own writes.
#   informed_by                   — REAL usage-outcome attribution (SP-8): written at
#       session-end (gated on SP8_ATTRIBUTION_ENABLE) to credit a session's outcome to
#       the memories it RECALLED. Folding it in is THE one line that makes the loop
#       functional — it lets the EWMA/regression engine see real usage, not just its
#       own bookkeeping. Safe by default: with the gate OFF no informed_by edges exist,
#       so behavior is byte-identical to the bookkeeping-only past.
_OUTCOME_PREDICATES = ("validated_as", "superseded_by", "informed_by")

# A sane positive clamp so the multiplicative recall weight can never go <=0 (an EWMA
# in [-1,1] maps weight to [1+(-1)*?]; we clamp defensively). The weight is
# 1.0 + EWMA(scores), and EWMA(scores) is bounded by [-1, 1] for scores in {-1,0,1},
# so weight is naturally in [0.0, 2.0]; we floor it just above 0 to keep ranking sane.
_WEIGHT_FLOOR = 0.01
_WEIGHT_CEIL = 2.0


def score_signal(signal) -> int:
    """The §5.2 score for one outcome_signal: +1 positive, -1 negative, 0 otherwise
    (an unknown signal or a NULL never silently counts as a win or a loss)."""
    if signal in POSITIVE_SIGNALS:
        return 1
    if signal in NEGATIVE_SIGNALS:
        return -1
    return 0


# --------------------------------------------------------------------------- #
# The reverse-edge outcome lookup (idx_links_dst).
# --------------------------------------------------------------------------- #

def linked_outcomes(conn, unit_id) -> list[dict]:
    """Return the unit's linked outcome events, OLDEST-first (ordered by event ts),
    each a dict {ts, outcome_signal}. Scans the DST-side reverse edge
    (idx_links_dst): the session_events linked to this memory via an outcome
    predicate, keeping only those that actually carry a signal.

    Fail-closed-to-empty: any read error returns [] (the caller then leaves the
    unit's weight unchanged — never invents evidence off a broken read)."""
    placeholders = ",".join("?" for _ in _OUTCOME_PREDICATES)
    try:
        rows = conn.execute(
            f"""
            SELECT se.ts AS ts, se.outcome_signal AS outcome_signal
              FROM links l
              JOIN session_events se ON se.id = CAST(l.src_id AS INTEGER)
             WHERE l.dst_kind = 'memory'
               AND l.dst_id = ?
               AND l.src_kind = 'session_event'
               AND l.predicate IN ({placeholders})
               AND se.outcome_signal IS NOT NULL
             ORDER BY se.ts ASC, se.id ASC
            """,
            (unit_id, *_OUTCOME_PREDICATES),
        ).fetchall()
    except Exception:
        return []
    return [{"ts": r["ts"], "outcome_signal": r["outcome_signal"]} for r in rows]


# --------------------------------------------------------------------------- #
# The recency-weighted EWMA fold.
# --------------------------------------------------------------------------- #

def ewma_of(outcomes, *, alpha: float = EWMA_ALPHA) -> float:
    """Fold an OLDEST-first list of outcome dicts into a recency-weighted EWMA of
    their scores. Starts neutral (0.0); each step ewma = alpha*score + (1-alpha)*ewma
    so the most-recent outcome carries the largest weight. Empty -> 0.0."""
    ewma = 0.0
    seen = False
    for o in outcomes:
        seen = True
        ewma = alpha * score_signal(o.get("outcome_signal")) + (1.0 - alpha) * ewma
    return ewma if seen else 0.0


def weight_from_ewma(ewma: float) -> float:
    """Map an EWMA in [-1, 1] to an outcome_weight: 1.0 + ewma, clamped to a sane
    positive band. Net-positive history -> >1.0 (promote); net-negative -> <1.0
    (demote); neutral/empty -> 1.0 (the inert default, unchanged)."""
    w = 1.0 + ewma
    if w < _WEIGHT_FLOOR:
        return _WEIGHT_FLOOR
    if w > _WEIGHT_CEIL:
        return _WEIGHT_CEIL
    return w


# --------------------------------------------------------------------------- #
# The aggregate write (the FIRST non-1.0 writer of memories.outcome_weight).
# --------------------------------------------------------------------------- #

def aggregate_unit(conn, unit_id, *, ts, set_weight_fn=set_outcome_weight) -> bool:
    """Fold a single unit's linked outcomes into its outcome_weight and write it
    back (the FIRST non-1.0 writer; audited by the engine).

    Returns True iff a weight was written, False on a no-op (no linked outcomes, or
    a fail-open swallow of a write error — the unit then keeps its prior weight).

    `set_weight_fn` is injectable so a test can force a write error (fail-open) and
    so the orchestrator can swap a dry-run no-op writer. Defaults to the engine
    `set_outcome_weight`.

    FAIL-OPEN: a write error degrades to a no-op (return False), never raises out."""
    outcomes = linked_outcomes(conn, unit_id)
    if not outcomes:
        # No evidence → leave the inert 1.0 default untouched (no spurious write).
        return False
    weight = weight_from_ewma(ewma_of(outcomes))
    try:
        set_weight_fn(
            conn, id=unit_id, weight=weight, ts=ts,
            reason=f"sp7 outcome aggregate (n={len(outcomes)}, ewma-weight={weight:.4f})")
    except Exception:
        # Fail-open: a write error must never wedge the maintenance run.
        return False
    return True


def _agent_authored_active_ids(conn) -> list[str]:
    """All units the loop is even allowed to aggregate over: agent-authored,
    currently active. (The provenance WALL — aggressive_wall.assert_mutable — gates
    the WRITE verbs; the aggregate is a read-mostly signal, but we still restrict it
    to the agent-authored set so the loop never reasons over human/pinned rows.)
    Fail-closed-to-empty on a read error."""
    try:
        rows = conn.execute(
            "SELECT id FROM memories "
            "WHERE created_by IN ('agent','background_review') "
            "AND status='active' AND pinned=0"
        ).fetchall()
    except Exception:
        return []
    return [r["id"] for r in rows]


def aggregate_all(conn, *, ts, set_weight_fn=set_outcome_weight) -> int:
    """Fold EVERY agent-authored active unit's outcome_weight (spec §5.2 step 0 of
    the Stage-2c pipeline). Returns the count of units that got a non-default write.

    FAIL-OPEN PER UNIT: a single unit erroring is skipped (logged via the per-unit
    fail-open in aggregate_unit), the rest still fold — one bad row never wedges the
    whole pass."""
    written = 0
    for uid in _agent_authored_active_ids(conn):
        try:
            if aggregate_unit(conn, uid, ts=ts, set_weight_fn=set_weight_fn):
                written += 1
        except Exception:
            # Belt-and-suspenders: aggregate_unit is already fail-open, but never let
            # an unexpected error from one unit abort the loop.
            continue
    return written


# --------------------------------------------------------------------------- #
# The regression signal (the §5.2 strict definition — the Stage-6 trigger).
# --------------------------------------------------------------------------- #

def _net_and_count(conn, unit_id) -> tuple[int, int]:
    """The (net score, count) of a unit's linked outcomes — the raw aggregate the
    regression test reasons over (distinct from the recency-weighted EWMA, which is
    for ranking; the regression uses the un-discounted net so a long bad streak is
    not recency-masked)."""
    outs = linked_outcomes(conn, unit_id)
    net = sum(score_signal(o.get("outcome_signal")) for o in outs)
    return net, len(outs)


def is_regression(conn, *, regressed_id, prior_id) -> bool:
    """The §5.2 REGRESSION test for one (edited/graduated) unit.

    A regression iff ALL hold:
      1. the new version has >= MIN_EVIDENCE linked outcomes (the noise floor — a
         single noisy bad outcome is NOT a regression);
      2. the new version's linked outcomes are NET-NEGATIVE (it is hurting);
      3. (when a prior version exists) the new version is BELOW the pre-edit
         baseline — its mean outcome score is worse than the prior's mean (the EDIT
         made it worse, not a pre-existing badness). A no-prior graduation (prior_id
         is None) skips condition 3: a freshly-graduated lesson that only produces
         losses is a regression on its own evidence.

    Fail-closed-to-NOT-a-regression: any uncertainty (read error, sub-floor
    evidence) returns False — the SAFE default is to NOT flag (reverting on noise is
    the destructive failure mode this guards against)."""
    new_net, new_n = _net_and_count(conn, regressed_id)

    # 1. Evidence floor — a sub-floor count is noise, never a regression.
    if new_n < MIN_EVIDENCE:
        return False

    # 2. Must be net-negative (the unit is actively hurting).
    if new_net >= 0:
        return False

    # 3. Below the pre-edit baseline (the edit made it WORSE than before).
    if prior_id is not None:
        prior_net, prior_n = _net_and_count(conn, prior_id)
        if prior_n == 0:
            # No prior evidence to compare against → treat like a no-prior unit:
            # the net-negative + floor conditions (already met) suffice.
            return True
        # Compare MEAN outcome score so unequal-length histories compare fairly.
        new_mean = new_net / new_n
        prior_mean = prior_net / prior_n
        if not (new_mean < prior_mean):
            # The new version is no worse than the prior baseline → the edit did not
            # cause the regression (the prior was already at-least-as-bad).
            return False

    return True


def detect_regressions(conn) -> list[dict]:
    """Scan the auto-edit `superseded_by` edges (old -> new) and return the pairs
    whose NEW version regressed (per `is_regression`) — the candidate list the §5.2
    self-reversion track (Stage 6) consumes.

    Each result is {regressed_id, prior_id} (prior_id is the old/superseded
    version). ONLY memory->memory superseded_by edges are auto-edit lineage (the
    event->memory superseded_by edges are evidence, not lineage), so the scan keys
    on src_kind='memory'. Fail-open-to-empty on a read error."""
    try:
        edges = conn.execute(
            "SELECT src_id, dst_id FROM links "
            "WHERE predicate='superseded_by' "
            "AND src_kind='memory' AND dst_kind='memory'"
        ).fetchall()
    except Exception:
        return []

    regressions: list[dict] = []
    for e in edges:
        prior_id = e["src_id"]      # the OLD (superseded) version
        new_id = e["dst_id"]        # the NEW (current) version
        try:
            if is_regression(conn, regressed_id=new_id, prior_id=prior_id):
                regressions.append({"regressed_id": new_id, "prior_id": prior_id})
        except Exception:
            # Fail-open per edge — one bad lineage row never aborts the scan.
            continue
    return regressions
