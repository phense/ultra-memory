"""Tests for aggressive_eval.py — SP-7 §6 (the EVAL GATE — hard + quality,
shadow-apply) — Stage 4 of the SP-7 build (spec §7 step 4).

`run_aggressive_eval` mirrors `scripts/judge_eval_backfill.py`'s ship-gate
(`run_eval` → confusion-matrix → a HARD negative gate (`gate_neg_pass = fp==0`)
+ a quality gate → `ship_pass`). SP-7's analog:

  (1) HARD gate (ZERO TOLERANCE): if ANY proposed action targets a forbidden
      (human / import / pinned) unit → gate_hard_pass=False → HALT THE WHOLE RUN
      + alert (the §4a stop-the-world, the `fp==0` analog). A single
      forbidden-target attempt is NOT a per-item skip — it stops everything.
      The check funnels through aggressive_wall.assert_mutable, which RE-READS
      the live row (never trusts an LLM-echoed field).

  (2) QUALITY gate (deterministic, NO LLM): shadow-apply each proposed edit to a
      TEMP-DB CLONE (never the live/source store), run a FROZEN retrieval probe
      set (queries-with-expected-top-hits) via unified_recall against
      (a) shadow-WITH-edit vs (b) the as-is baseline; REJECT the edit if it
      REGRESSES top-k overlap / rank-correlation below θ vs baseline (STRICT —
      reject ANY regression, per the resolved fork D). A rejected edit is kept as
      a PROPOSED action in the digest, NOT applied. No live store ever sees an
      un-evaluated edit.

HARD INVARIANTS under test:
  * the hard gate FAILS-THE-RUN on a forbidden (human/import/pinned) target —
    gate_hard_pass=False, the whole run halts (not a per-item skip);
  * the hard gate re-reads the LIVE row (an LLM-echoed 'agent' on a human row is
    ignored);
  * the quality gate REJECTS a probe-regressing shadow edit AND PASSES a
    non-regressing one;
  * the shadow store NEVER mutates the live/source store (a clone is edited; the
    source row is byte-unchanged after the eval);
  * a rejected edit is reported as proposed-but-rejected (digest), never applied;
  * strict θ (fork D): ANY measurable probe regression rejects;
  * fail-open: an eval error degrades to a SAFE reject (do-no-harm: an
    un-evaluable edit is treated as not-passing), never raises out;
  * NO LLM call / NO anthropic SDK import (the quality gate is deterministic
    retrieval over the shadow; a guard test asserts it).

These tests NEVER touch the live memory.db, NEVER spawn `claude`, NEVER load a
real embedder (a DETERMINISTIC bag-of-words STUB embedder drives recall — no
fastembed, no network), and run against a temp DB clone + synthetic agent-authored
memories + a frozen probe set.
"""
import hashlib
import math
import sys
from pathlib import Path

import pytest


from ultra_memory.maintenance import aggressive_eval as ae  # noqa: E402
from ultra_memory.maintenance import aggressive_wall as aw  # noqa: E402
from ultra_memory import memory_lib  # noqa: E402


TS = "2026-05-31T00:00:00Z"

# unified_recall recalls memories of the TRUSTED-caller allowed types
# (knowledge_mcp.ALL_TYPES = project/reference/user/feedback — NOT 'learning').
# The probe corpus uses a recallable type so the frozen probe set actually
# retrieves its targets; the aggressive WALL still gates on provenance, not type.
PROBE_TYPE = "reference"

EMBED_DIM = 384


def _tok_bucket(tok: str) -> int:
    """A STABLE token→bucket hash (hashlib, NOT the PYTHONHASHSEED-salted builtin
    `hash()`) so the stub embedder is deterministic ACROSS processes — the eval
    result is the same whether the test runs alone or inside the full suite."""
    digest = hashlib.sha256(("sp7-stub:" + tok).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % EMBED_DIM


def _stub_embedder(texts):
    """A DETERMINISTIC bag-of-words stub embedder — NO fastembed, NO network.

    Each text is tokenized and hashed into 384 buckets (a STABLE hashlib hash, so
    the result is reproducible across processes); the L2-normalized bucket-count
    vector makes cosine similarity reflect term OVERLAP reproducibly. So a probe
    query whose distinctive terms survive an edit keeps a high cosine to its
    target, while a gutted edit (terms stripped) demonstrably drops the cosine —
    which is exactly what lets the quality gate DETECT a regression deterministically
    (no real model, same result every run, every process)."""
    out = []
    for text in texts:
        vec = [0.0] * EMBED_DIM
        for tok in str(text).lower().split():
            tok = "".join(ch for ch in tok if ch.isalnum())
            if not tok:
                continue
            vec[_tok_bucket(tok)] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        out.append([v / norm for v in vec])
    return out


# --------------------------------------------------------------------------- #
# Fixture helpers — a synthetic store: agent-authored memories the probe set can
# retrieve. The quality gate clones THIS db, shadow-applies an edit, and compares
# unified_recall over the clone-with-edit vs the as-is clone.
# --------------------------------------------------------------------------- #

def _open_temp_db(tmp_path, name="memory.db"):
    return memory_lib.open_memory_db(str(tmp_path / name))


def _db_path(conn) -> str:
    """The on-disk path of an open sqlite connection (for cloning)."""
    for _id, name, fname in conn.execute("PRAGMA database_list").fetchall():
        if name == "main":
            return fname
    raise RuntimeError("no main db path")


def _save(conn, *, id, created_by="agent", body="a lesson", title="L", pinned=False,
          type=PROBE_TYPE):
    memory_lib.save_memory(
        conn, id=id, type=type, title=title, body=body, ts=TS,
        created_by=created_by)
    if pinned:
        memory_lib.set_pinned(conn, id=id, pinned=True, ts=TS, reason="test pin")
    return id


def _body(conn, mem_id):
    return conn.execute(
        "SELECT body FROM memories WHERE id=?", (mem_id,)).fetchone()["body"]


def _status(conn, mem_id):
    return conn.execute(
        "SELECT status FROM memories WHERE id=?", (mem_id,)).fetchone()["status"]


# A frozen probe set: queries the synthetic store answers, with the memory id
# expected to top the recall. These never change run-to-run (the §6 "frozen
# fixture of queries-with-expected-top-hits"). Memory-only recall (no
# unified_index rows) is the byte-identity path — deterministic, no embedder.
def _seed_probe_corpus(conn):
    """Seed agent-authored memories whose distinctive terms the probe set hits."""
    _save(conn, id="vix-term",
          title="VIX spike regime",
          body="When the VIX spikes above thirty volatility regime turns risk-off "
               "and credit spreads widen sharply across the curve.")
    _save(conn, id="theta-term",
          title="Theta decay accelerates",
          body="Short option premium decays fastest in the final week before "
               "expiration as theta accelerates into the gamma zone.")
    _save(conn, id="macd-term",
          title="MACD crossover hedge",
          body="A weekly MACD bearish crossover is the trigger to add a long put "
               "hedge overlay against the long stock book.")


def _probes():
    """The frozen probe set: (query, expected_top_id). Each query's distinctive
    terms should rank its target memory first under memory-only BM25 recall."""
    return [
        {"query": "VIX spike volatility regime risk-off", "expect": "vix-term"},
        {"query": "theta decay short premium expiration gamma", "expect": "theta-term"},
        {"query": "weekly MACD bearish crossover put hedge overlay", "expect": "macd-term"},
    ]


# --------------------------------------------------------------------------- #
# Proposed-action records — the opaque plan the LLM (§5.5) hands the eval. The
# eval re-reads the live row for the hard gate; the quality-gate body is the
# proposed NEW text the shadow applies.
# --------------------------------------------------------------------------- #

def _edit_action(*, old_id, new_body, new_title="L (edited)", evidence="trace:ev",
                 echoed_created_by="agent"):
    return {
        "verb": "auto_edit",
        "old_id": old_id,
        "new_body": new_body,
        "new_title": new_title,
        "evidence": evidence,
        "echoed_created_by": echoed_created_by,
    }


# =========================================================================== #
# 1. The HARD gate (zero tolerance — the §4a stop-the-world / fp==0 analog)
# =========================================================================== #

def test_hard_gate_fails_run_on_forbidden_human_target(tmp_path):
    """A single proposed action targeting a HUMAN unit → gate_hard_pass=False →
    the whole run halts (not a per-item skip). The §4a stop-the-world, mirroring
    judge_eval_backfill's gate_neg_pass=(fp==0): zero tolerance."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m-agent", created_by="agent")
    _save(conn, id="m-human", created_by="human")
    plan = {"edits": [
        _edit_action(old_id="m-agent", new_body="fine"),
        _edit_action(old_id="m-human", new_body="ILLEGAL — human target"),
    ]}
    rep = ae.run_aggressive_eval(conn, plan, probes=_probes(), embedder=None)
    assert rep["gate_hard_pass"] is False
    assert rep["halt"] is True
    # A forbidden target is reported (for the digest + the alert).
    assert any("m-human" in str(v) for v in rep["forbidden_targets"])
    # HALT means NOTHING is admitted for apply — not even the legal action.
    assert rep["admitted"] == [] or rep["admitted"] == {}


def test_hard_gate_passes_when_all_targets_mutable(tmp_path):
    """When every proposed action targets an agent/background_review unmutated
    unit, gate_hard_pass=True and the run does not halt."""
    conn = _open_temp_db(tmp_path)
    _seed_probe_corpus(conn)
    _save(conn, id="m1", created_by="agent", body="lesson one")
    _save(conn, id="m2", created_by="background_review", body="lesson two")
    plan = {"edits": [
        _edit_action(old_id="m1", new_body="lesson one"),
        _edit_action(old_id="m2", new_body="lesson two"),
    ]}
    rep = ae.run_aggressive_eval(conn, plan, probes=_probes(), embedder=None)
    assert rep["gate_hard_pass"] is True
    assert rep["halt"] is False


def test_hard_gate_rereads_live_row_ignores_echoed_provenance(tmp_path):
    """The hard gate funnels through assert_mutable, which RE-READS the live row.
    A human row carrying an LLM-echoed created_by='agent' is STILL forbidden —
    the echoed hint is ignored (prompt-injection / hallucination cannot make a
    human row mutable)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m-human", created_by="human")
    plan = {"edits": [
        _edit_action(old_id="m-human", new_body="x", echoed_created_by="agent"),
    ]}
    rep = ae.run_aggressive_eval(conn, plan, probes=_probes(), embedder=None)
    assert rep["gate_hard_pass"] is False
    assert rep["halt"] is True


def test_hard_gate_catches_pinned_agent_target(tmp_path):
    """A pinned agent-authored unit is still forbidden (the independent §4a pin
    condition) — the hard gate halts the run."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m-pin", created_by="agent", pinned=True)
    plan = {"edits": [_edit_action(old_id="m-pin", new_body="x")]}
    rep = ae.run_aggressive_eval(conn, plan, probes=_probes(), embedder=None)
    assert rep["gate_hard_pass"] is False
    assert rep["halt"] is True


def test_hard_gate_covers_reversion_and_quarantine_targets(tmp_path):
    """The hard gate inspects EVERY action class — a forbidden reversion or
    quarantine target also halts the run (not just edits)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m-agent", created_by="agent")
    _save(conn, id="m-human", created_by="human")
    # A reversion whose regressed_id is a human row.
    plan = {"reversions": [{"verb": "revert", "regressed_id": "m-human",
                            "prior_id": None}]}
    rep = ae.run_aggressive_eval(conn, plan, probes=_probes(), embedder=None)
    assert rep["gate_hard_pass"] is False

    # A quarantine pair with a human member.
    plan2 = {"quarantines": [{"verb": "quarantine", "id_a": "m-agent",
                              "id_b": "m-human", "reason": "x"}]}
    rep2 = ae.run_aggressive_eval(conn, plan2, probes=_probes(), embedder=None)
    assert rep2["gate_hard_pass"] is False


# =========================================================================== #
# 2. The QUALITY gate (deterministic shadow-apply + frozen probe set)
# =========================================================================== #

def test_quality_gate_passes_non_regressing_edit(tmp_path):
    """A non-regressing edit (it sharpens a lesson WITHOUT removing the terms the
    probe set retrieves on) clears the quality gate and is admitted for apply."""
    conn = _open_temp_db(tmp_path)
    _seed_probe_corpus(conn)
    # Edit vix-term: keep ALL the probe terms, just add sharpening text.
    plan = {"edits": [_edit_action(
        old_id="vix-term",
        new_body="When the VIX spikes above thirty volatility regime turns "
                 "risk-off and credit spreads widen sharply across the curve. "
                 "Sharpened: this is most reliable in a rising-rate macro window.")]}
    rep = ae.run_aggressive_eval(conn, plan, probes=_probes(), embedder=_stub_embedder)
    assert rep["gate_hard_pass"] is True
    # The edit passed the quality gate → admitted.
    admitted_ids = {a["old_id"] for a in rep["admitted"]}
    assert "vix-term" in admitted_ids
    # And it is recorded as a passing edit (not rejected).
    assert not any(r["old_id"] == "vix-term" for r in rep["rejected"])


def test_quality_gate_rejects_probe_regressing_edit(tmp_path):
    """A probe-REGRESSING edit (it guts the distinctive terms the probe set
    retrieves on, so the target memory no longer tops its query) is REJECTED —
    kept as proposed-but-rejected in the digest, NOT admitted for apply.
    Strict θ (fork D): any measurable regression rejects."""
    conn = _open_temp_db(tmp_path)
    _seed_probe_corpus(conn)
    # Gut vix-term: strip EVERY distinctive query term → its probe query no longer
    # retrieves it first (a measurable regression vs the as-is baseline).
    plan = {"edits": [_edit_action(
        old_id="vix-term",
        new_body="generic placeholder text with none of the distinctive terms")]}
    rep = ae.run_aggressive_eval(conn, plan, probes=_probes(), embedder=_stub_embedder)
    assert rep["gate_hard_pass"] is True               # provenance fine
    # The regressing edit is REJECTED — not admitted.
    admitted_ids = {a["old_id"] for a in rep["admitted"]}
    assert "vix-term" not in admitted_ids
    # It survives as a PROPOSED-but-rejected action (digested for the operator).
    assert any(r["old_id"] == "vix-term" for r in rep["rejected"])


def test_quality_gate_mixed_plan_admits_clean_rejects_dirty(tmp_path):
    """A plan with one clean + one regressing edit: the clean one is admitted, the
    dirty one rejected — the gate is PER-EDIT (an independent shadow per edit)."""
    conn = _open_temp_db(tmp_path)
    _seed_probe_corpus(conn)
    plan = {"edits": [
        _edit_action(old_id="theta-term",
                     new_body="Short option premium decays fastest in the final "
                              "week before expiration as theta accelerates into "
                              "the gamma zone. Extra note: monitor weekend decay."),
        _edit_action(old_id="macd-term",
                     new_body="unrelated filler paragraph with none of the original "
                              "distinctive indicator or overlay terminology at all"),
    ]}
    rep = ae.run_aggressive_eval(conn, plan, probes=_probes(), embedder=_stub_embedder)
    admitted_ids = {a["old_id"] for a in rep["admitted"]}
    rejected_ids = {r["old_id"] for r in rep["rejected"]}
    assert "theta-term" in admitted_ids
    assert "macd-term" in rejected_ids
    assert "macd-term" not in admitted_ids


# =========================================================================== #
# 3. The shadow store NEVER mutates the live/source store
# =========================================================================== #

def test_shadow_never_mutates_source_store(tmp_path):
    """The quality gate edits a TEMP-DB CLONE, never the live/source store. After a
    full eval (even one that admits an edit), the SOURCE rows are byte-unchanged —
    only a later (separate) apply step touches the live store."""
    conn = _open_temp_db(tmp_path)
    _seed_probe_corpus(conn)
    before_body = _body(conn, "vix-term")
    before_status = _status(conn, "vix-term")
    before_count = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]

    plan = {"edits": [_edit_action(
        old_id="vix-term",
        new_body=before_body + " Sharpened tail keeping every probe term intact.")]}
    rep = ae.run_aggressive_eval(conn, plan, probes=_probes(), embedder=_stub_embedder)
    assert rep["gate_hard_pass"] is True

    # SOURCE store is byte-identical — the shadow clone absorbed the edit.
    assert _body(conn, "vix-term") == before_body
    assert _status(conn, "vix-term") == before_status
    after_count = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
    assert after_count == before_count             # no new shadow version leaked in


def test_clone_is_independent_file(tmp_path):
    """clone_store produces a SEPARATE on-disk db; mutating the clone leaves the
    source untouched (the structural guarantee the quality gate relies on)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="m1", created_by="agent", body="original")
    clone_path = ae.clone_store(_db_path(conn), tmp_path)
    assert Path(clone_path).exists()
    assert str(clone_path) != _db_path(conn)
    clone = memory_lib.open_memory_db(clone_path)
    clone.execute("UPDATE memories SET body='MUTATED' WHERE id='m1'")
    clone.commit()
    # Source unchanged.
    assert _body(conn, "m1") == "original"


# =========================================================================== #
# 4. The frozen-probe metric — top-k overlap + rank correlation
# =========================================================================== #

def test_probe_metric_identical_lists_no_regression():
    """Two identical ranked lists → no regression (overlap=1.0, rank-corr=1.0)."""
    base = ["a", "b", "c"]
    assert ae.regresses(base, base) is False


def test_probe_metric_lost_top_hit_is_regression():
    """Losing the expected top hit from the top-k → a regression (strict θ)."""
    base = ["a", "b", "c"]
    after = ["x", "y", "z"]              # the baseline top hits all dropped out
    assert ae.regresses(base, after) is True


def test_probe_metric_reordered_below_threshold_is_regression():
    """A rank-correlation drop below θ (a reshuffle that demotes the top hit) is a
    regression under strict θ."""
    base = ["a", "b", "c", "d"]
    after = ["d", "c", "b", "a"]        # fully reversed → rank-corr collapses
    assert ae.regresses(base, after) is True


def test_strict_theta_rejects_any_regression():
    """Fork D: strict — even a single-position demotion of a baseline hit counts
    as a regression (do-no-harm). A small but real degradation is not tolerated."""
    base = ["a", "b", "c"]
    after = ["a", "c"]                  # 'b' dropped out of the top-k entirely
    assert ae.regresses(base, after) is True


# --------------------------------------------------------------------------- #
# 4b. The PROBE-ANCHORED metric — a demotion of the EXPECTED hit is a regression,
# but incidental tail churn of UNRELATED docs is not (the over-strictness fix).
# --------------------------------------------------------------------------- #

def test_expected_hit_demotion_is_regression():
    """The expected hit moving to a WORSE rank → a regression (the edit hurt the
    known-good target)."""
    base = ["target", "b", "c"]
    after = ["b", "target", "c"]       # target demoted #1 -> #2
    assert ae.expected_hit_regressed("target", base, after) is True


def test_expected_hit_dropout_is_regression():
    """The expected hit dropping out of the top-k → a regression."""
    base = ["target", "b", "c"]
    after = ["b", "c", "d"]            # target gone
    assert ae.expected_hit_regressed("target", base, after) is True


def test_expected_hit_held_with_tail_churn_is_not_regression():
    """The CRUX: the expected hit keeps its rank while UNRELATED docs reshuffle in
    the tail (which any legitimate added term causes) → NOT a regression. A
    do-no-harm gate must not reject a clean edit over incidental tail churn."""
    base = ["target", "b", "c"]
    after = ["target", "c", "b"]       # target still #1; only b/c (unrelated) swap
    assert ae.expected_hit_regressed("target", base, after) is False


def test_expected_hit_promotion_is_not_regression():
    """The expected hit PROMOTED (a better rank) is an improvement, not a
    regression."""
    base = ["b", "target", "c"]
    after = ["target", "b", "c"]       # target #2 -> #1
    assert ae.expected_hit_regressed("target", base, after) is False


def test_expected_hit_absent_at_baseline_is_not_regression():
    """If the expected hit was not even retrieved at baseline (a stale fixture for
    that probe), there is no baseline signal to regress against → not a
    regression."""
    base = ["b", "c", "d"]
    after = ["b", "c", "d"]
    assert ae.expected_hit_regressed("target", base, after) is False


def test_expected_none_falls_back_to_generic_metric():
    """A probe with no declared `expect` falls back to the whole-list strict metric
    so an un-anchored probe is still guarded."""
    base = ["a", "b", "c"]
    after = ["x", "y", "z"]            # everything dropped → generic regression
    assert ae.expected_hit_regressed(None, base, after) is True


# =========================================================================== #
# 5. Fail-open — an eval error degrades to a SAFE reject, never raises
# =========================================================================== #

def test_quality_gate_failopen_is_safe_reject(tmp_path, monkeypatch):
    """Fail-open + fail-CLOSED-to-safety: if the shadow scoring raises, the edit is
    treated as NOT-passing (a safe reject — do no harm), never admitted, and the
    run never raises out into the maintenance pass."""
    conn = _open_temp_db(tmp_path)
    _seed_probe_corpus(conn)

    def _boom(*a, **k):
        raise RuntimeError("shadow scoring blew up")

    # Break the shadow scorer — the edit must be safely rejected, not admitted.
    monkeypatch.setattr(ae, "_score_edit_on_shadow", _boom)
    plan = {"edits": [_edit_action(
        old_id="vix-term", new_body=_body(conn, "vix-term") + " harmless tail")]}
    rep = ae.run_aggressive_eval(conn, plan, probes=_probes(), embedder=None)
    # Hard gate is independent of the shadow scorer → still passes provenance.
    assert rep["gate_hard_pass"] is True
    # The un-evaluable edit is a SAFE reject (do-no-harm), not admitted.
    assert "vix-term" not in {a["old_id"] for a in rep["admitted"]}


def test_run_eval_never_raises_on_malformed_plan(tmp_path):
    """A malformed plan (a non-list action class, a missing field) degrades
    fail-open — the eval returns a report, it never raises out."""
    conn = _open_temp_db(tmp_path)
    _seed_probe_corpus(conn)
    rep = ae.run_aggressive_eval(conn, {"edits": "not-a-list"},
                                 probes=_probes(), embedder=None)
    assert isinstance(rep, dict)
    assert "gate_hard_pass" in rep


# =========================================================================== #
# 5b. FIX 4 — empty probe set is FAIL-CLOSED in live (admits NOTHING)
# =========================================================================== #

def test_empty_probes_live_admits_nothing(tmp_path):
    """FIX 4: a well-formed, non-regressing edit with an EMPTY probe set, run LIVE
    (apply=True), must NOT be admitted — the strict quality gate has zero coverage,
    so it fails-CLOSED (admit nothing) rather than silently admitting every edit.
    The report must surface the no-probe-coverage hold."""
    conn = _open_temp_db(tmp_path)
    _seed_probe_corpus(conn)
    plan = {"edits": [_edit_action(
        old_id="vix-term", new_body=_body(conn, "vix-term") + " sharpening tail")]}

    rep = ae.run_aggressive_eval(conn, plan, probes=[], embedder=_stub_embedder,
                                 apply=True)
    assert rep["gate_hard_pass"] is True               # provenance fine
    # With no probe coverage in LIVE, the edit is HELD — not admitted.
    assert rep["admitted"] == [], (
        f"empty-probe LIVE eval must admit NOTHING; got {rep['admitted']}")
    # The edit survives in the digest as a hold, and the hold is flagged.
    assert any(r.get("old_id") == "vix-term" for r in rep["rejected"])
    assert rep.get("no_probe_coverage") is True
    assert "probe" in " ".join(rep.get("notes", [])).lower()


def test_nonempty_probes_live_admits_clean_edit(tmp_path):
    """FIX 4 (the other side): with a NON-EMPTY probe set the same clean edit is
    admitted as before — the fail-closed behavior is ONLY triggered by empty probes."""
    conn = _open_temp_db(tmp_path)
    _seed_probe_corpus(conn)
    plan = {"edits": [_edit_action(
        old_id="vix-term",
        new_body="When the VIX spikes above thirty volatility regime turns "
                 "risk-off and credit spreads widen sharply across the curve. "
                 "Sharpened in a rising-rate macro window.")]}
    rep = ae.run_aggressive_eval(conn, plan, probes=_probes(), embedder=_stub_embedder,
                                 apply=True)
    assert rep["gate_hard_pass"] is True
    assert "vix-term" in {a["old_id"] for a in rep["admitted"]}
    assert not rep.get("no_probe_coverage")


def test_run_edit_track_empty_probes_live_applies_nothing(tmp_path):
    """End-to-end through run_edit_track: empty probes + apply=True → applied is
    empty (the fail-closed eval admitted nothing, so the bounded apply has nothing
    to do). The source row body is unchanged."""
    from ultra_memory.maintenance import aggressive_edit as aedit  # noqa: E402
    conn = _open_temp_db(tmp_path)
    _seed_probe_corpus(conn)
    before = _body(conn, "vix-term")
    plan = {"edits": [_edit_action(
        old_id="vix-term", new_body=before + " sharpening tail")]}

    res = aedit.run_edit_track(conn, plan, probes=[], embedder=_stub_embedder,
                               ts=TS, apply=True)
    assert res["applied"] == [], f"empty-probe live apply must be a no-op; got {res['applied']}"
    assert res["admitted"] == []
    # The live row is untouched.
    assert _body(conn, "vix-term") == before


# =========================================================================== #
# 6. OAuth-only + no-LLM guards (the quality gate is deterministic)
# =========================================================================== #

def test_eval_module_no_anthropic_sdk_import():
    """The eval module makes NO model call (the quality gate is deterministic
    retrieval over the shadow). No anthropic SDK / API import anywhere."""
    src = Path(ae.__file__).read_text()
    for forbidden in ("import anthropic", "from anthropic", "ANTHROPIC_API_KEY",
                      "messages.create", "cache_control", "api.anthropic.com",
                      "claude_cli", "run_claude"):
        assert forbidden not in src, f"OAuth/no-LLM violation: {forbidden!r} in eval"


def test_eval_module_never_deletes():
    """The eval module is non-destructive to the LIVE store — it only CLONES + reads
    + shadow-mutates the throwaway clone. No rm / delete against the source."""
    src = Path(ae.__file__).read_text()
    for forbidden in ("os.remove(", "shutil.rmtree(", ".unlink(",
                      "memory_lib.delete(", "rm -rf", "DROP TABLE"):
        assert forbidden not in src, f"destructive call {forbidden!r} in eval"
