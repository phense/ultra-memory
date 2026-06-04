"""Tests for aggressive_revert.py — SP-7 §5.2 mechanism (OUTCOME-BASED
SELF-REVERSION) — Stage 6 of the SP-7 build (spec §7 step 6), under the RESOLVED
fork A: reversion is PROPOSE-FOR-PETER (NOT fully autonomous).

The self-reversion track is the second of the three aggressive capabilities,
built on top of the safety wall (Stages 1-4) + the outcome signal (Stage 3). It
is the GEPA/Pareto *post-commit* defense: a graduated/auto-edited unit whose
linked downstream outcomes REGRESSED (made things worse than the prior version)
is reverted/demoted — but, per fork A, the DEFAULT is to PROPOSE the reversion to
the digest WITHOUT applying it (the operator confirms). Auto-edit + quarantine run
autonomously; reversion proposes — it is the verb most likely to be itself wrong
(a regression may be noise, not the lesson's fault — Risk §9.3).

HARD INVARIANTS under test (spec §7 step 6 / §8 / fork A):
  * a detected regression EMITS a proposed reversion to the digest WITHOUT
    applying (the propose-for-the-operator default — apply NOTHING on the default path);
  * the proposal carries the regressed unit + its prior version + the regression
    evidence (so the operator can adjudicate from the digest);
  * the reversion MECHANISM, when INVOKED (the operator-confirm path), reverts to the
    prior version (a pure FSM flip: regressed→'reverted', prior→'active') — an
    archive-never-delete reversible transition;
  * a graduated-then-regressed unit with NO prior version DEMOTES to 'quarantined'
    (out of recall) rather than reverting to nothing;
  * a `reverted_from` link (regressed -> prior) is written on the confirm path;
  * BOUNDED to MAX_REVERSIONS_PER_RUN, halt-on-exceed (applies none of the class);
  * provenance-gated — a forbidden (human/import/pinned) target halts the run
    (the §4a stop-the-world, zero tolerance, NOT a per-item skip);
  * NO LLM call anywhere (the regression signal is deterministic) — NO anthropic
    SDK import (OAuth-only, asserted);
  * archive-never-delete: no destructive call anywhere (asserted);
  * fail-open: any error degrades to a no-op (no reversion applied), never raises.

These tests NEVER touch the live memory.db, NEVER spawn `claude` (the track makes
NO LLM call at all), NEVER load a real embedder, and run against a temp DB +
synthetic agent-authored memories + linked outcome traces.
"""
import sys
from pathlib import Path

import pytest


from ultra_memory.maintenance import aggressive_outcomes as ao  # noqa: E402
from ultra_memory.maintenance import aggressive_revert as arv  # noqa: E402
from ultra_memory.maintenance import aggressive_wall as aw  # noqa: E402
from ultra_memory import memory_lib  # noqa: E402


TS = "2026-05-31T00:00:00Z"
PROBE_TYPE = "reference"


# --------------------------------------------------------------------------- #
# Fixture helpers — agent-authored memories + an auto-edit lineage (old -> new
# via superseded_by) + linked outcome traces so the regression detector triggers.
# --------------------------------------------------------------------------- #

def _open_temp_db(tmp_path, name="memory.db"):
    return memory_lib.open_memory_db(str(tmp_path / name))


def _save(conn, *, id, created_by="agent", body="a lesson", title="L", pinned=False,
          type=PROBE_TYPE, status=None):
    memory_lib.save_memory(
        conn, id=id, type=type, title=title, body=body, ts=TS, created_by=created_by)
    if pinned:
        memory_lib.set_pinned(conn, id=id, pinned=True, ts=TS, reason="test pin")
    if status is not None:
        memory_lib.set_status(conn, id=id, status=status, ts=TS, reason="test seed")
    return id


def _event(conn, *, session_id, outcome_signal, ts=TS, title="ev"):
    memory_lib.record_session_event(
        conn, session_id=session_id, kind="skill_learning_candidate", title=title,
        ts=ts, detail="d", outcome_signal=outcome_signal)
    return int(conn.execute(
        "SELECT id FROM session_events ORDER BY id DESC LIMIT 1").fetchone()["id"])


def _link_outcomes(conn, *, unit_id, signals, predicate="validated_as",
                   ts_base="2026-05-{:02d}T00:00:00Z"):
    """Wire N events + outcome edges to a unit (the trace the regression reads)."""
    for i, sig in enumerate(signals):
        ts = ts_base.format(min(28, i + 1))
        ev = _event(conn, session_id=f"sess-{unit_id}-{i}", outcome_signal=sig, ts=ts,
                    title=f"ev-{unit_id}-{i}")
        memory_lib.record_link(
            conn, src_kind="session_event", src_id=str(ev), predicate=predicate,
            dst_kind="memory", dst_id=unit_id, ts=ts)


def _supersede(conn, *, old_id, new_id):
    """Wire the auto-edit lineage edge old -> new (the detector's scan key)."""
    memory_lib.record_link(
        conn, src_kind="memory", src_id=old_id, predicate="superseded_by",
        dst_kind="memory", dst_id=new_id, evidence="sp7-auto-edit", ts=TS)


def _row(conn, mem_id):
    return conn.execute(
        "SELECT status, supersedes, body, created_by FROM memories WHERE id=?",
        (mem_id,)).fetchone()


def _seed_regressed_lineage(conn, *, old_id="u-old", new_id="u-new",
                            old_signals=None, new_signals=None):
    """Seed an auto-edit lineage where the NEW version REGRESSED below the prior:
    the prior (old) had good outcomes; the new version (post-edit) has >=
    MIN_EVIDENCE net-negative outcomes worse than the prior's mean. The old is a
    'redirect' (the consolidate state); the new is 'active'."""
    # The old version graduated good, then was superseded (redirect, supersedes=new).
    _save(conn, id=old_id, created_by="agent", body="GOOD original lesson")
    _save(conn, id=new_id, created_by="background_review", body="BAD edited lesson")
    # consolidate would normally do this; we set it explicitly for the fixture.
    memory_lib.consolidate(conn, loser_id=old_id, canonical_id=new_id,
                           reason="test edit", ts=TS)
    _supersede(conn, old_id=old_id, new_id=new_id)
    # The prior version's outcomes were good (positive mean).
    _link_outcomes(conn, unit_id=old_id,
                   signals=old_signals or (["tests_passed"] * 10))
    # The new version's outcomes regressed (>= MIN_EVIDENCE, net-negative, worse).
    _link_outcomes(conn, unit_id=new_id,
                   signals=new_signals or (["tests_failed"] * 12))
    return old_id, new_id


# =========================================================================== #
# 1. The propose-for-the-operator DEFAULT — a detected regression PROPOSES, applies NONE
# =========================================================================== #

def test_detect_regression_proposes_without_applying(tmp_path):
    """The fork-A default: a detected regression EMITS a proposed reversion to the
    digest WITHOUT applying it. apply=False (the default) plans + proposes but
    flips NO status — the operator confirms before any reversion lands."""
    conn = _open_temp_db(tmp_path)
    old_id, new_id = _seed_regressed_lineage(conn)
    result = arv.run_revert_track(conn, ts=TS)   # apply defaults to False (propose)
    # A proposal was emitted for the regressed lineage.
    proposed_ids = {p["regressed_id"] for p in result["proposed"]}
    assert new_id in proposed_ids
    # Nothing was applied (propose-for-the-operator): both rows untouched.
    assert result["applied"] == []
    assert _row(conn, new_id)["status"] == "active"      # regressed still active
    assert _row(conn, old_id)["status"] == "redirect"    # prior still redirected


def test_proposal_carries_regressed_prior_and_evidence(tmp_path):
    """The proposal carries the regressed unit, its prior version, AND the
    regression evidence (net score + outcome count) — everything the operator needs to
    adjudicate the reversion straight from the digest."""
    conn = _open_temp_db(tmp_path)
    old_id, new_id = _seed_regressed_lineage(conn)
    result = arv.run_revert_track(conn, ts=TS)
    prop = next(p for p in result["proposed"] if p["regressed_id"] == new_id)
    assert prop["prior_id"] == old_id
    # The evidence is grounded in the deterministic regression signal.
    ev = prop["evidence"]
    assert ev["regressed_net"] < 0                      # the new version is hurting
    assert ev["regressed_n"] >= ao.MIN_EVIDENCE         # past the noise floor
    assert ev["prior_net"] > ev["regressed_net"]        # worse than the prior


def test_no_regression_proposes_nothing(tmp_path):
    """A healthy auto-edit lineage (the new version's outcomes are good) yields NO
    proposal — the track only surfaces genuine regressions."""
    conn = _open_temp_db(tmp_path)
    _seed_regressed_lineage(
        conn, old_id="u-old", new_id="u-new",
        old_signals=["tests_passed"] * 10,
        new_signals=["tests_passed"] * 12)            # the edit IMPROVED it
    result = arv.run_revert_track(conn, ts=TS)
    assert result["proposed"] == []
    assert result["applied"] == []


def test_noisy_single_bad_outcome_is_not_a_regression(tmp_path):
    """A sub-MIN_EVIDENCE handful of bad outcomes is NOISE, not a regression — the
    track proposes NOTHING (Risk §9.3: reverting on noise destroys good lessons)."""
    conn = _open_temp_db(tmp_path)
    _seed_regressed_lineage(
        conn, old_id="u-old", new_id="u-new",
        old_signals=["tests_passed"] * 10,
        new_signals=["tests_failed"] * 3)             # only 3 bad → below floor
    result = arv.run_revert_track(conn, ts=TS)
    assert result["proposed"] == []


# =========================================================================== #
# 2. The reversion MECHANISM — the operator-confirm path (apply=True) reverts
# =========================================================================== #

def test_confirm_reverts_to_prior_version_fsm_flip(tmp_path):
    """The operator-confirm path (apply=True) reverts to the prior version: a pure FSM
    flip — the regressed unit demotes to 'reverted' (out of recall), the prior
    re-activates. Archive-never-delete: nothing is deleted, both rows survive."""
    conn = _open_temp_db(tmp_path)
    old_id, new_id = _seed_regressed_lineage(conn)
    result = arv.run_revert_track(conn, ts=TS, apply=True)
    applied_ids = {a["regressed_id"] for a in result["applied"]}
    assert new_id in applied_ids
    # The FSM flip: regressed -> 'reverted', prior -> 'active'.
    assert _row(conn, new_id)["status"] == "reverted"
    assert _row(conn, old_id)["status"] == "active"
    # Both rows survive (archive-never-delete) — bytes intact.
    assert _row(conn, new_id)["body"] == "BAD edited lesson"
    assert _row(conn, old_id)["body"] == "GOOD original lesson"


def test_confirm_writes_reverted_from_link(tmp_path):
    """The confirm path writes a `reverted_from` edge (regressed -> prior) — the
    audit trail of the reversion."""
    conn = _open_temp_db(tmp_path)
    old_id, new_id = _seed_regressed_lineage(conn)
    arv.run_revert_track(conn, ts=TS, apply=True)
    link = conn.execute(
        "SELECT predicate, dst_id FROM links "
        "WHERE src_id=? AND predicate='reverted_from'", (new_id,)).fetchone()
    assert link is not None
    assert link["dst_id"] == old_id


def test_confirm_demotes_no_prior_graduation_to_quarantined(tmp_path):
    """A graduated-then-regressed unit with NO prior version (no superseded_by
    lineage — a fresh graduation that only produced losses) DEMOTES to 'quarantined'
    (out of recall) rather than reverting to nothing. Surfaced as a no-prior
    proposal, applied on confirm."""
    conn = _open_temp_db(tmp_path)
    # A freshly-graduated lesson (no prior), net-negative past the floor.
    _save(conn, id="u-grad", created_by="background_review", body="bad graduation")
    _link_outcomes(conn, unit_id="u-grad", signals=["tests_failed"] * 12)
    result = arv.run_revert_track(conn, ts=TS, apply=True,
                                  include_graduations=True)
    applied_ids = {a["regressed_id"] for a in result["applied"]}
    assert "u-grad" in applied_ids
    # No prior to fall back to → demoted to 'quarantined' (out of recall).
    assert _row(conn, "u-grad")["status"] == "quarantined"


def test_no_prior_proposal_marks_demote_not_revert(tmp_path):
    """A no-prior regressed graduation is PROPOSED with prior_id=None and the
    'demote' action (so the digest tells the operator it will be quarantined, not reverted
    to a non-existent prior)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="u-grad", created_by="background_review", body="bad graduation")
    _link_outcomes(conn, unit_id="u-grad", signals=["tests_failed"] * 12)
    result = arv.run_revert_track(conn, ts=TS, include_graduations=True)
    prop = next(p for p in result["proposed"] if p["regressed_id"] == "u-grad")
    assert prop["prior_id"] is None
    assert prop["action"] == "demote"               # quarantine, not revert


# =========================================================================== #
# 3. The safety wall — provenance gate + bounds (halt-on-exceed) on confirm
# =========================================================================== #

def test_confirm_blocks_forbidden_target_halts_run(tmp_path):
    """On the confirm path, a forbidden (human) target in the reversion plan HALTS
    the whole run (the §4a stop-the-world, zero tolerance) — NOTHING is applied,
    not even a legal reversion in the same batch. The hard gate re-reads the live
    row; it never trusts an LLM-echoed field (there is no LLM here, but the gate is
    the same apply-path enforcement)."""
    conn = _open_temp_db(tmp_path)
    # A legal regressed lineage + a hand-built reversion plan that ALSO targets a
    # human unit as the 'prior' (the forbidden re-activation target).
    old_id, new_id = _seed_regressed_lineage(conn)
    _save(conn, id="u-human", created_by="human", body="human rule")
    plan = {"reversions": [
        {"regressed_id": new_id, "prior_id": old_id, "action": "revert"},
        {"regressed_id": new_id, "prior_id": "u-human", "action": "revert"},
    ]}
    with pytest.raises(aw.ForbiddenTargetError):
        arv.apply_reversions(conn, plan["reversions"], ts=TS)
    # Zero-tolerance halt: NOTHING applied — the legal lineage is untouched too.
    assert _row(conn, new_id)["status"] == "active"
    assert _row(conn, old_id)["status"] == "redirect"
    assert _row(conn, "u-human")["status"] == "active"


def test_confirm_respects_max_reversions_bound(tmp_path):
    """The confirm apply is bounded to MAX_REVERSIONS_PER_RUN, halt-on-exceed: a
    plan with MORE reversions than the cap applies NONE of the class (not the first
    N) — the §4c stop-and-ask."""
    conn = _open_temp_db(tmp_path)
    from ultra_memory.maintenance.aggressive_bounds import MAX_REVERSIONS_PER_RUN
    n = MAX_REVERSIONS_PER_RUN + 1
    plan_revs = []
    for i in range(n):
        old_id, new_id = _seed_regressed_lineage(
            conn, old_id=f"o{i}", new_id=f"v{i}")
        plan_revs.append({"regressed_id": new_id, "prior_id": old_id,
                          "action": "revert"})
    applied = arv.apply_reversions(conn, plan_revs, ts=TS,
                                   max_reversions=MAX_REVERSIONS_PER_RUN)
    # Halt-on-exceed: NONE applied.
    assert applied == []
    for i in range(n):
        assert _row(conn, f"v{i}")["status"] == "active"    # all untouched


def test_confirm_under_bound_applies_all(tmp_path):
    """A reversion plan at/under the cap applies every reversion."""
    conn = _open_temp_db(tmp_path)
    from ultra_memory.maintenance.aggressive_bounds import MAX_REVERSIONS_PER_RUN
    n = MAX_REVERSIONS_PER_RUN
    plan_revs = []
    for i in range(n):
        old_id, new_id = _seed_regressed_lineage(
            conn, old_id=f"o{i}", new_id=f"v{i}")
        plan_revs.append({"regressed_id": new_id, "prior_id": old_id,
                          "action": "revert"})
    applied = arv.apply_reversions(conn, plan_revs, ts=TS,
                                   max_reversions=MAX_REVERSIONS_PER_RUN)
    assert len(applied) == n
    for i in range(n):
        assert _row(conn, f"v{i}")["status"] == "reverted"   # all reverted


# =========================================================================== #
# 4. Fail-open + OAuth-only + archive-never-delete guards
# =========================================================================== #

def test_run_revert_track_failopen_never_raises(tmp_path):
    """Fail-open: a broken read (a closed connection) degrades to a no-op result,
    never raises out into the maintenance run."""
    conn = _open_temp_db(tmp_path)
    conn.close()
    result = arv.run_revert_track(conn, ts=TS)
    assert isinstance(result, dict)
    assert result["proposed"] == []
    assert result["applied"] == []


def test_revert_module_no_anthropic_sdk_import():
    """The reversion track makes NO LLM call (the regression signal is
    deterministic) and NEVER imports the anthropic SDK / API — OAuth-only by
    construction."""
    src = Path(arv.__file__).read_text()
    for forbidden in ("import anthropic", "from anthropic", "ANTHROPIC_API_KEY",
                      "messages.create", "cache_control", "api.anthropic.com"):
        assert forbidden not in src, f"OAuth-only violation: {forbidden!r} in revert"


def test_revert_module_never_deletes():
    """Static guard: the reversion module never `rm`s / hard-deletes — every verb
    is a reversible FSM transition (archive-never-delete)."""
    src = Path(arv.__file__).read_text()
    for forbidden in ("os.remove(", "shutil.rmtree(", ".unlink(",
                      "memory_lib.delete(", ".delete(tier", "rm -rf", "DROP TABLE"):
        assert forbidden not in src, f"destructive call {forbidden!r} in revert"


def test_dryrun_path_applies_nothing_even_with_regressions(tmp_path):
    """Belt-and-suspenders: even if a caller passes apply=True for the confirm
    mechanism, the propose-for-the-operator DEFAULT (apply omitted/False) must apply
    nothing — re-asserting the fork-A default is propose, not auto-apply."""
    conn = _open_temp_db(tmp_path)
    _seed_regressed_lineage(conn)
    # Default call (no apply kwarg) — must propose, apply nothing.
    result = arv.run_revert_track(conn, ts=TS)
    assert result["proposed"] != []
    assert result["applied"] == []
