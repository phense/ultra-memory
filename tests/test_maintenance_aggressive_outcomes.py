"""Tests for aggressive_outcomes.py — SP-7 §5.2 (the deterministic, NO-LLM
outcome-weight EWMA aggregate + the regression signal).

This is the GEPA/Pareto CORE's *signal* layer: for each agent-authored unit, fold
the `outcome_signal`s of its linked `session_events` (via the SP-6 `validated_as`
graph + the new `superseded_by` edges, looked up on the reverse-edge
`idx_links_dst`) into a recency-weighted EWMA, written back to
`memories.outcome_weight` through the engine `set_outcome_weight` (the FIRST
non-1.0 writer of that 0004 column). NO LLM here — it is the deterministic
periodic aggregate (SP-3 D13's "deterministic capture → periodic aggregate").

Scoring (spec §5.2):
    +1  {tests_passed, backtest_improved, trade_win, commit_landed}
    -1  {tests_failed, backtest_regressed, trade_loss, reverted}
A REGRESSION = an edit/graduation whose post-edit linked outcomes are net-NEGATIVE
AND below the pre-edit baseline (the unit made things WORSE than before the loop
touched it). A MIN_EVIDENCE floor (>=10, config) ensures a single noisy bad
outcome is NOT a regression (spec §5.2 + Risk §9.3: "a regression from a handful
of outcomes may be luck, not the lesson's fault").

HARD INVARIANTS under test:
  * linked outcomes fold into outcome_weight (recency-weighted EWMA);
  * the MIN_EVIDENCE floor rejects a single (or sub-floor) noisy bad outcome —
    NOT flagged as a regression;
  * a REAL regression (>= MIN_EVIDENCE, net-negative, AND below the pre-edit
    baseline) IS detected;
  * the weight write is the FIRST non-1.0 writer + is audited (engine _audit row);
  * fail-open: an aggregate error degrades to a no-op (the unit keeps its prior
    weight), never raises out into the maintenance run;
  * NO LLM call / NO anthropic SDK import (the deterministic apply path; a guard).

These tests NEVER touch the live memory.db, NEVER spawn `claude`, NEVER load a
real embedder. They run against a temp DB + synthetic agent-authored memories +
synthetic session_events carrying outcome_signal + synthetic validated_as edges.
"""
import sys
from pathlib import Path

import pytest


from ultra_memory.maintenance import aggressive_outcomes as ao  # noqa: E402
from ultra_memory import memory_lib  # noqa: E402


TS = "2026-05-31T00:00:00Z"


# --------------------------------------------------------------------------- #
# Fixture helpers — build a synthetic store: agent-authored memories + session
# events carrying outcome_signal + validated_as edges (event -> memory, the SP-6
# direction). The reverse-edge idx_links_dst is what the aggregator scans.
# --------------------------------------------------------------------------- #

def _open_temp_db(tmp_path):
    return memory_lib.open_memory_db(str(tmp_path / "memory.db"))


def _save(conn, *, id, created_by="agent", body="a lesson", title="L"):
    memory_lib.save_memory(
        conn, id=id, type="learning", title=title, body=body, ts=TS,
        created_by=created_by)
    return id


def _event(conn, *, session_id, outcome_signal, ts, kind="skill_learning_candidate",
           title="ev", detail="d"):
    """Insert a session_event carrying an outcome_signal and return its INTEGER id
    (the value SP-6 stringifies into the validated_as link's src_id)."""
    memory_lib.record_session_event(
        conn, session_id=session_id, kind=kind, title=title, ts=ts,
        detail=detail, outcome_signal=outcome_signal)
    row = conn.execute(
        "SELECT id FROM session_events ORDER BY id DESC LIMIT 1").fetchone()
    return int(row["id"])


def _link_event_to_unit(conn, *, event_id, unit_id, predicate="validated_as", ts=TS):
    """The SP-6 edge direction: src=session_event -> dst=memory. The aggregator
    must find these via the dst-side reverse edge (idx_links_dst)."""
    memory_lib.record_link(
        conn, src_kind="session_event", src_id=str(event_id),
        predicate=predicate, dst_kind="memory", dst_id=unit_id, ts=ts)


def _link_unit_outcomes(conn, *, unit_id, signals, ts_base="2026-05-{:02d}T00:00:00Z"):
    """Wire a list of outcome_signals to a unit via N events + N validated_as edges.
    Events are timestamped increasingly so recency-weighting has an ordering."""
    for i, sig in enumerate(signals):
        ts = ts_base.format(min(28, i + 1))
        ev = _event(conn, session_id=f"sess-{unit_id}-{i}", outcome_signal=sig, ts=ts,
                    title=f"ev-{unit_id}-{i}")
        _link_event_to_unit(conn, event_id=ev, unit_id=unit_id, ts=ts)


def _weight(conn, unit_id):
    return conn.execute(
        "SELECT outcome_weight FROM memories WHERE id=?", (unit_id,)).fetchone()[0]


# --------------------------------------------------------------------------- #
# 1. Scoring map — the spec §5.2 +1/-1 table
# --------------------------------------------------------------------------- #

def test_scoring_map_positive_signals():
    for sig in ("tests_passed", "backtest_improved", "trade_win", "commit_landed"):
        assert ao.score_signal(sig) == 1


def test_scoring_map_negative_signals():
    for sig in ("tests_failed", "backtest_regressed", "trade_loss", "reverted"):
        assert ao.score_signal(sig) == -1


def test_scoring_map_unknown_signal_is_neutral():
    """An unrecognized / None signal contributes 0 (neutral) — it never silently
    counts as a win or a loss."""
    assert ao.score_signal("some_unmapped_signal") == 0
    assert ao.score_signal(None) == 0


def test_min_evidence_floor_is_conservative():
    """Fork B 'conservative': MIN_EVIDENCE >= 10 (spec §5.2 + §10 fork B)."""
    assert ao.MIN_EVIDENCE >= 10


# --------------------------------------------------------------------------- #
# 2. Linked-outcome lookup (the reverse-edge idx_links_dst scan)
# --------------------------------------------------------------------------- #

def test_linked_outcomes_found_via_reverse_edge(tmp_path):
    """The aggregator reads a unit's linked outcome_signals through the dst-side
    reverse edge: links WHERE dst=memory:<unit> AND predicate IN
    (validated_as, superseded_by) -> src session_event -> its outcome_signal."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="u1")
    _link_unit_outcomes(conn, unit_id="u1",
                        signals=["tests_passed", "trade_win", "tests_failed"])
    outs = ao.linked_outcomes(conn, "u1")
    # Three events, ordered by ts; each carries (ts, outcome_signal).
    sigs = [o["outcome_signal"] for o in outs]
    assert sorted(sigs) == sorted(["tests_passed", "trade_win", "tests_failed"])


def test_linked_outcomes_also_follows_superseded_by(tmp_path):
    """superseded_by edges (auto-edit's edge) are part of the outcome graph too —
    a regressed auto-edit's outcomes flow back through them (spec §5.2)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="u-new")
    ev = _event(conn, session_id="s", outcome_signal="tests_failed", ts=TS)
    _link_event_to_unit(conn, event_id=ev, unit_id="u-new", predicate="superseded_by")
    outs = ao.linked_outcomes(conn, "u-new")
    assert [o["outcome_signal"] for o in outs] == ["tests_failed"]


def test_linked_outcomes_ignores_unrelated_predicates(tmp_path):
    """An edge of an unrelated predicate (e.g. 'contradicts') is NOT an outcome
    edge — it must not pollute the aggregate."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="u2")
    ev = _event(conn, session_id="s", outcome_signal="trade_loss", ts=TS)
    _link_event_to_unit(conn, event_id=ev, unit_id="u2", predicate="contradicts")
    assert ao.linked_outcomes(conn, "u2") == []


def test_linked_outcomes_skips_events_with_null_signal(tmp_path):
    """A linked event with NO outcome_signal (NULL) contributes nothing — the
    aggregate only sees events that actually carry a signal."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="u3")
    ev = _event(conn, session_id="s", outcome_signal=None, ts=TS)
    _link_event_to_unit(conn, event_id=ev, unit_id="u3")
    assert ao.linked_outcomes(conn, "u3") == []


# --------------------------------------------------------------------------- #
# 3. The recency-weighted EWMA fold
# --------------------------------------------------------------------------- #

def test_all_wins_folds_above_one(tmp_path):
    """A unit whose linked outcomes are all wins folds to an outcome_weight > 1.0
    (promotes recall rank) — and it is the FIRST non-1.0 write (was the inert 1.0
    default before)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="winner")
    assert _weight(conn, "winner") == 1.0          # inert 0004 default before SP-7
    _link_unit_outcomes(conn, unit_id="winner", signals=["tests_passed"] * 12)
    ao.aggregate_unit(conn, "winner", ts=TS)
    assert _weight(conn, "winner") > 1.0


def test_all_losses_folds_below_one(tmp_path):
    """A unit whose linked outcomes are all losses folds below 1.0 (demotes rank)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="loser")
    _link_unit_outcomes(conn, unit_id="loser", signals=["tests_failed"] * 12)
    ao.aggregate_unit(conn, "loser", ts=TS)
    assert _weight(conn, "loser") < 1.0


def test_ewma_is_recency_weighted(tmp_path):
    """Recency-weighted (spec §5.2 'EWMA'): a unit that RECENTLY turned positive
    after early losses folds HIGHER than one that recently turned negative after
    early wins, even with the same multiset of signals — recent outcomes dominate."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="improving")
    _save(conn, id="declining")
    # Same 12-signal multiset (6 fail, 6 pass), opposite recency order.
    improving = ["tests_failed"] * 6 + ["tests_passed"] * 6   # recent = wins
    declining = ["tests_passed"] * 6 + ["tests_failed"] * 6   # recent = losses
    _link_unit_outcomes(conn, unit_id="improving", signals=improving)
    _link_unit_outcomes(conn, unit_id="declining", signals=declining)
    ao.aggregate_unit(conn, "improving", ts=TS)
    ao.aggregate_unit(conn, "declining", ts=TS)
    assert _weight(conn, "improving") > _weight(conn, "declining")


def test_aggregate_no_outcomes_leaves_weight_unchanged(tmp_path):
    """A unit with NO linked outcomes keeps its prior weight (no spurious write to
    a non-default value off zero evidence)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="lonely")
    ao.aggregate_unit(conn, "lonely", ts=TS)
    assert _weight(conn, "lonely") == 1.0


# --------------------------------------------------------------------------- #
# 4. The weight write is the FIRST non-1.0 writer + audited
# --------------------------------------------------------------------------- #

def test_weight_write_is_audited(tmp_path):
    """The outcome_weight write rides the engine _audit row (op='outcome_weight')
    — Peter is in the audit loop for the regression signal too (spec §2/§5.2)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="audited")
    _link_unit_outcomes(conn, unit_id="audited", signals=["tests_passed"] * 12)
    ao.aggregate_unit(conn, "audited", ts=TS)
    row = conn.execute(
        "SELECT op, target_id FROM audit_log WHERE op='outcome_weight' "
        "AND target_id='audited'").fetchone()
    assert row is not None


def test_aggregate_all_writes_only_units_with_evidence(tmp_path):
    """aggregate_all folds every agent-authored unit; a unit with no outcomes is
    left at 1.0 (no write), a unit with outcomes gets its non-1.0 weight."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="with-ev")
    _save(conn, id="no-ev")
    _link_unit_outcomes(conn, unit_id="with-ev", signals=["trade_win"] * 11)
    n = ao.aggregate_all(conn, ts=TS)
    assert _weight(conn, "with-ev") != 1.0
    assert _weight(conn, "no-ev") == 1.0
    assert n >= 1                                  # at least the one with evidence


# --------------------------------------------------------------------------- #
# 5. The regression detector — the GEPA core's TRIGGER
# --------------------------------------------------------------------------- #

def test_regression_detected_real(tmp_path):
    """A REAL regression: an auto-edited unit with >= MIN_EVIDENCE post-edit linked
    outcomes that are net-NEGATIVE AND below the pre-edit baseline (the prior
    version did better). is_regression(...) -> True (spec §5.2)."""
    conn = _open_temp_db(tmp_path)
    # Prior (good) version: net-positive baseline.
    _save(conn, id="prior", body="good lesson")
    _link_unit_outcomes(conn, unit_id="prior", signals=["tests_passed"] * 10)
    # New (regressed) version: >= MIN_EVIDENCE outcomes, net-negative.
    _save(conn, id="new", created_by="background_review", body="regressed edit")
    _link_unit_outcomes(conn, unit_id="new", signals=["tests_failed"] * 12)
    assert ao.is_regression(conn, regressed_id="new", prior_id="prior") is True


def test_regression_floor_rejects_single_noisy_bad_outcome(tmp_path):
    """The MIN_EVIDENCE floor: a single (or sub-floor) bad outcome is NOT a
    regression — it is noise, not signal (spec §5.2 + Risk §9.3). Auto-reverting on
    noise would DESTROY good lessons, so the floor is a hard gate."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="prior2", body="good")
    _link_unit_outcomes(conn, unit_id="prior2", signals=["tests_passed"] * 10)
    _save(conn, id="new2", created_by="background_review")
    # ONE bad outcome — below MIN_EVIDENCE.
    _link_unit_outcomes(conn, unit_id="new2", signals=["tests_failed"])
    assert ao.is_regression(conn, regressed_id="new2", prior_id="prior2") is False


def test_regression_requires_net_negative(tmp_path):
    """Even with >= MIN_EVIDENCE outcomes, a NET-POSITIVE new version is not a
    regression (the unit is helping, not hurting) — both conditions are required."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="prior3")
    _link_unit_outcomes(conn, unit_id="prior3", signals=["tests_passed"] * 10)
    _save(conn, id="new3", created_by="background_review")
    # 10 wins, 2 losses over MIN_EVIDENCE — net positive → NOT a regression.
    _link_unit_outcomes(conn, unit_id="new3",
                        signals=["tests_passed"] * 10 + ["tests_failed"] * 2)
    assert ao.is_regression(conn, regressed_id="new3", prior_id="prior3") is False


def test_regression_requires_below_baseline(tmp_path):
    """A net-negative new version that is STILL not below the pre-edit baseline (the
    prior was even worse) is NOT a regression caused by the edit — the strict
    definition is 'net-negative AND below the pre-edit baseline' (spec §5.2)."""
    conn = _open_temp_db(tmp_path)
    # Prior was already terrible (net more negative than new).
    _save(conn, id="prior4")
    _link_unit_outcomes(conn, unit_id="prior4", signals=["tests_failed"] * 14)
    _save(conn, id="new4", created_by="background_review")
    # New is net-negative but LESS bad than prior → the edit did not make it worse.
    _link_unit_outcomes(conn, unit_id="new4",
                        signals=["tests_failed"] * 7 + ["tests_passed"] * 5)
    assert ao.is_regression(conn, regressed_id="new4", prior_id="prior4") is False


def test_regression_no_prior_uses_self_evidence(tmp_path):
    """A graduated-then-regressed unit with NO prior version (prior_id=None): the
    regression test is purely 'net-negative AND >= MIN_EVIDENCE' (there is no
    baseline to be below; a freshly-graduated lesson that only produces losses is a
    regression on its own evidence)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="grad", created_by="background_review")
    _link_unit_outcomes(conn, unit_id="grad", signals=["trade_loss"] * 12)
    assert ao.is_regression(conn, regressed_id="grad", prior_id=None) is True
    # And a net-positive no-prior unit is not a regression.
    _save(conn, id="grad-ok", created_by="background_review")
    _link_unit_outcomes(conn, unit_id="grad-ok", signals=["trade_win"] * 12)
    assert ao.is_regression(conn, regressed_id="grad-ok", prior_id=None) is False


def test_informed_by_usage_outcomes_flip_regression_true(tmp_path):
    """SP-8 HEADLINE — the fold that makes the self-learning loop FUNCTIONAL.

    `informed_by` edges (written at session-end, gated on SP8_ATTRIBUTION_ENABLE)
    attribute a session's REAL usage outcome to the memories it recalled. The loop's
    EWMA/regression engine only counts edges in `_OUTCOME_PREDICATES`; until SP-8
    folded `informed_by` into that tuple it counted ONLY its own bookkeeping edges
    (`validated_as` graduation + `superseded_by` lineage), so real usage outcomes
    were INERT.

    This test proves the fold: a graduated unit whose ONLY bookkeeping edge is a
    single (positive) `validated_as` graduation is NOT a regression (sub-floor, no
    negative evidence). Once ≥ MIN_EVIDENCE NET-NEGATIVE `informed_by` usage outcomes
    accrue, is_regression flips True — and it flips ONLY because the fold added
    `informed_by` to `_OUTCOME_PREDICATES`. With the pre-fold tuple, linked_outcomes
    would return the validated_as-only set (one positive outcome → net-positive,
    sub-floor) → is_regression False → the final assert-True would FAIL (RED)."""
    conn = _open_temp_db(tmp_path)
    # 1. A graduated agent-authored active unit + its single graduation edge: ONE
    #    validated_as carrying a positive signal. No prior version (a fresh
    #    graduation), so is_regression uses self-evidence.
    _save(conn, id="grad-fn", created_by="background_review", body="a graduated lesson")
    grad_ev = _event(conn, session_id="grad-sess", outcome_signal="commit_landed", ts=TS)
    _link_event_to_unit(conn, event_id=grad_ev, unit_id="grad-fn",
                        predicate="validated_as")

    # 2. Today's behavior: only the (positive) validated_as edge → net-positive,
    #    sub-floor → NOT a regression. This holds on BOTH the pre- and post-fold
    #    tuple (validated_as is in both), so it is a stable precondition.
    assert ao.is_regression(conn, regressed_id="grad-fn", prior_id=None) is False

    # 3. Now ≥ MIN_EVIDENCE NET-NEGATIVE real USAGE outcomes accrue via informed_by
    #    edges (the SP-8 session-end attribution), shaped exactly as linked_outcomes
    #    reads them: session_event --informed_by--> memory, each carrying a negative
    #    outcome_signal. NOTE we wire ONLY informed_by here (not validated_as), so the
    #    sole reason is_regression can flip is the fold counting informed_by.
    for i in range(ao.MIN_EVIDENCE):
        ev = _event(conn, session_id=f"usage-{i}", outcome_signal="trade_loss",
                    ts=f"2026-05-{min(28, i + 1):02d}T00:00:00Z", title=f"usage-{i}")
        _link_event_to_unit(conn, event_id=ev, unit_id="grad-fn",
                            predicate="informed_by")

    # 4. THE KEY ASSERTION: with the informed_by usage outcomes present, is_regression
    #    flips True — ONLY because the fold added 'informed_by' to _OUTCOME_PREDICATES.
    #    On the pre-fold tuple, linked_outcomes would ignore the informed_by edges and
    #    see only the single positive validated_as outcome → net-positive, sub-floor →
    #    is_regression False → THIS assert would fail (the RED-before-the-fold proof).
    assert "informed_by" in ao._OUTCOME_PREDICATES, (
        "the fold must add 'informed_by' to _OUTCOME_PREDICATES (SP-8 B3)")
    outs = ao.linked_outcomes(conn, "grad-fn")
    assert sum(1 for o in outs if o["outcome_signal"] == "trade_loss") >= ao.MIN_EVIDENCE, (
        "the informed_by usage outcomes must be visible to linked_outcomes")
    assert ao.is_regression(conn, regressed_id="grad-fn", prior_id=None) is True


def test_detect_regressions_returns_candidates(tmp_path):
    """detect_regressions scans superseded_by edges (auto-edit's old->new) and
    returns the (regressed_id, prior_id) pairs whose new version regressed — the
    candidate list the §5.2 reversion track (stage 6) consumes."""
    conn = _open_temp_db(tmp_path)
    # An auto-edit pair: old 'prior5' superseded_by 'new5'; the new one regressed.
    _save(conn, id="prior5", body="good")
    _save(conn, id="new5", created_by="background_review", body="regressed")
    _link_unit_outcomes(conn, unit_id="prior5", signals=["tests_passed"] * 10)
    _link_unit_outcomes(conn, unit_id="new5", signals=["tests_failed"] * 12)
    # The auto-edit superseded_by edge old -> new (the §5.1 apply path writes this).
    memory_lib.record_link(
        conn, src_kind="memory", src_id="prior5", predicate="superseded_by",
        dst_kind="memory", dst_id="new5", ts=TS)
    regs = ao.detect_regressions(conn)
    pairs = {(r["regressed_id"], r["prior_id"]) for r in regs}
    assert ("new5", "prior5") in pairs


# --------------------------------------------------------------------------- #
# 6. Fail-open — an aggregate error degrades to a no-op, never raises
# --------------------------------------------------------------------------- #

def test_aggregate_unit_failopen_keeps_prior_weight(tmp_path):
    """Fail-open (project rule + spec §4f): if the set_outcome_weight write raises,
    aggregate_unit degrades to a no-op (the unit keeps its prior weight) and never
    raises out into the maintenance run."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="ff")
    _link_unit_outcomes(conn, unit_id="ff", signals=["tests_passed"] * 12)

    def _boom(*a, **k):
        raise RuntimeError("write blew up")

    # Inject a failing weight writer — the aggregate must swallow it.
    res = ao.aggregate_unit(conn, "ff", ts=TS, set_weight_fn=_boom)
    assert res is False                              # reported no-op, did not raise
    assert _weight(conn, "ff") == 1.0                # prior weight intact


def test_aggregate_all_failopen_on_bad_unit(tmp_path):
    """A single unit erroring does not wedge the whole aggregate_all pass — it
    skips that unit and folds the rest (fail-open per unit)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="ok-unit")
    _link_unit_outcomes(conn, unit_id="ok-unit", signals=["tests_passed"] * 11)

    calls = {"n": 0}
    real = memory_lib.set_outcome_weight

    def _flaky(conn, *, id, weight, ts, reason="outcome aggregate"):
        calls["n"] += 1
        if id == "ok-unit" and calls["n"] == 1:
            # First call for ok-unit: simulate a transient error, then it is skipped.
            raise RuntimeError("transient")
        return real(conn, id=id, weight=weight, ts=ts, reason=reason)

    # Should not raise even though the (only) unit errors.
    n = ao.aggregate_all(conn, ts=TS, set_weight_fn=_flaky)
    assert isinstance(n, int)                        # returned a count, did not crash


# --------------------------------------------------------------------------- #
# OAuth-only guard — the aggregator is deterministic (NO LLM), no SDK import
# --------------------------------------------------------------------------- #

def test_outcomes_module_no_anthropic_sdk_import():
    src = Path(ao.__file__).read_text()
    for forbidden in ("import anthropic", "from anthropic", "ANTHROPIC_API_KEY",
                      "messages.create", "cache_control", "api.anthropic.com",
                      "claude_cli", "run_claude"):
        assert forbidden not in src, f"OAuth/no-LLM violation: {forbidden!r} in outcomes"


def test_outcomes_module_never_deletes():
    """The aggregator is non-destructive: it only writes outcome_weight + reads
    edges. NO rm / delete anywhere (archive-never-delete is structural)."""
    src = Path(ao.__file__).read_text()
    for forbidden in ("os.remove(", "shutil.rmtree(", ".unlink(",
                      "memory_lib.delete(", "rm -rf", "DROP TABLE", "DELETE FROM"):
        assert forbidden not in src, f"destructive call {forbidden!r} in outcomes"
