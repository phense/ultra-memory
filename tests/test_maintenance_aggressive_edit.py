"""Tests for aggressive_edit.py — SP-7 §5.1 (GEPA-lite TRACE-REFLECTIVE
AUTO-EDIT) — Stage 5 of the SP-7 build (spec §7 step 5).

The auto-edit track is the FIRST of the three aggressive capabilities, built on
top of the safety wall (Stages 1-4). It:

  1. SELECTS candidates with NO LLM (spec §5.1 trigger): an agent-authored unit
     with >= MIN_EVIDENCE linked outcomes whose outcome_weight is mixed /
     trending-down (the "sharpen it" zone — below a threshold but NOT yet a hard
     regression), OR a conservative-pass near-dup-not-merge pair.
  2. REFLECTS via ONE batched OAuth call (an INJECTED runner — tests never spawn
     `claude`): feeds the unit + its TRACE (linked session_events + their
     outcome_signals + dedup context) and asks for a TARGETED diff
     (reword/sharpen/merge-two/correct), NOT a free rewrite. Each diff MUST cite
     the trace evidence that motivates it — an UNGROUNDED "improvement" is
     rejected at plan-parse (the eval-reject the spec calls for).
  3. APPLIES (only through the wall + eval + bounds): via
     aggressive_wall.apply_auto_edit — a redirect-preserving new version
     (save_memory(created_by='background_review') + consolidate so the OLD is a
     recoverable redirect) + a superseded_by link carrying the trace refs.
     Provenance-gated (assert_mutable RE-READS the live row), bounded
     (MAX_EDITS), eval-gated upstream.

HARD INVARIANTS under test (spec §7 step 5 / §8):
  * a trace-reflective diff (a STUBBED plan from the injected runner) is applied
    via redirect-preserving versioning (the OLD version recoverable);
  * a superseded_by link is written carrying the trace refs as evidence;
  * bounded — over the MAX_EDITS cap halts-on-exceed (applies none of the class);
  * eval-gated — a degrading (probe-regressing) edit is REJECTED, never applied;
  * assert_mutable blocks a forbidden (human/import/pinned) target — and a single
    forbidden target halts the whole run (the §4a stop-the-world, not a skip);
  * an UNGROUNDED diff (no trace-evidence citation) is dropped at plan-parse;
  * the reflection is ONE batched call through the injected runner (no `claude`);
  * fail-open: any error degrades to a no-op (no edit applied), never raises out;
  * NO anthropic SDK import (OAuth-only); the diff is a TARGETED edit, not a free
    rewrite.

These tests NEVER touch the live memory.db, NEVER spawn `claude` (a fake runner is
injected), NEVER load a real embedder (a deterministic bag-of-words STUB drives
recall), and run against a temp DB + synthetic agent-authored memories + traces +
a frozen probe set.
"""
import hashlib
import json
import math
import sys
from pathlib import Path

import pytest


from ultra_memory.maintenance import aggressive_edit as aed  # noqa: E402
from ultra_memory.maintenance import aggressive_wall as aw  # noqa: E402
from ultra_memory import memory_lib  # noqa: E402


TS = "2026-05-31T00:00:00Z"

# A valid fake OAuth env so run_claude's _child_env() does not raise OAuthViolation
# (the reflection call now routes through the ultra_memory.claude_cli chokepoint,
# which requires CLAUDE_CODE_OAUTH_TOKEN + refuses ANTHROPIC_API_KEY). Tests inject
# this so the injected runner is actually reached; no `claude` is ever spawned.
FAKE_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "tok-test"}

# unified_recall recalls memories of the TRUSTED-caller allowed types (NOT
# 'learning'); the probe corpus uses a recallable type so the frozen probe set
# actually retrieves its targets. The aggressive WALL still gates on provenance.
PROBE_TYPE = "reference"

EMBED_DIM = 384


# --------------------------------------------------------------------------- #
# A deterministic bag-of-words STUB embedder — NO fastembed, NO network (the
# eval quality gate needs an embedder; this keeps the result reproducible).
# --------------------------------------------------------------------------- #

def _tok_bucket(tok: str) -> int:
    digest = hashlib.sha256(("sp7-stub:" + tok).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % EMBED_DIM


def _stub_embedder(texts):
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
# Fixture helpers — agent-authored memories + traces (events + outcome_signals +
# validated_as edges) so the no-LLM trigger has evidence to select on.
# --------------------------------------------------------------------------- #

def _open_temp_db(tmp_path, name="memory.db"):
    return memory_lib.open_memory_db(str(tmp_path / name))


def _save(conn, *, id, created_by="agent", body="a lesson", title="L", pinned=False,
          type=PROBE_TYPE, weight=None):
    memory_lib.save_memory(
        conn, id=id, type=type, title=title, body=body, ts=TS, created_by=created_by)
    if pinned:
        memory_lib.set_pinned(conn, id=id, pinned=True, ts=TS, reason="test pin")
    if weight is not None:
        memory_lib.set_outcome_weight(conn, id=id, weight=weight, ts=TS,
                                      reason="test seed weight")
    return id


def _event(conn, *, session_id, outcome_signal, ts=TS, title="ev", detail="d"):
    memory_lib.record_session_event(
        conn, session_id=session_id, kind="skill_learning_candidate", title=title,
        ts=ts, detail=detail, outcome_signal=outcome_signal)
    return int(conn.execute(
        "SELECT id FROM session_events ORDER BY id DESC LIMIT 1").fetchone()["id"])


def _link_outcomes(conn, *, unit_id, signals, ts_base="2026-05-{:02d}T00:00:00Z"):
    """Wire N events + validated_as edges to a unit (the trace the trigger reads)."""
    for i, sig in enumerate(signals):
        ts = ts_base.format(min(28, i + 1))
        ev = _event(conn, session_id=f"sess-{unit_id}-{i}", outcome_signal=sig, ts=ts,
                    title=f"ev-{unit_id}-{i}")
        memory_lib.record_link(
            conn, src_kind="session_event", src_id=str(ev), predicate="validated_as",
            dst_kind="memory", dst_id=unit_id, ts=ts)


def _row(conn, mem_id):
    return conn.execute(
        "SELECT status, supersedes, body, created_by FROM memories WHERE id=?",
        (mem_id,)).fetchone()


def _body(conn, mem_id):
    r = conn.execute("SELECT body FROM memories WHERE id=?", (mem_id,)).fetchone()
    return r["body"] if r else None


# A mixed/trending-down trace: enough evidence (>= MIN_EVIDENCE), net mixed (some
# wins, more recent losses) → outcome_weight below the edit-trigger threshold but
# NOT a hard regression (not net-negative). This is the §5.1 "sharpen it" zone.
def _mixed_trending_down(n_pos, n_neg):
    return ["tests_passed"] * n_pos + ["tests_failed"] * n_neg


# --------------------------------------------------------------------------- #
# A fake injected runner — mimics ultra_memory.claude_cli.run_claude's CONTRACT
# (a subprocess.run-compatible callable returning a CompletedProcess), so the
# reflection's batched call goes through it and NEVER spawns `claude`.
# --------------------------------------------------------------------------- #

class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def _make_runner(plan_json, *, calls=None):
    """Return a subprocess.run-compatible fake that records each invocation and
    returns the canned plan JSON as stdout. `calls` (a list) accumulates the
    command tuples so a test can assert exactly ONE batched call was made and that
    it never invoked an anthropic API."""
    calls = calls if calls is not None else []

    def runner(cmd, *a, **k):
        calls.append(cmd)
        return _FakeProc(plan_json if isinstance(plan_json, str)
                         else json.dumps(plan_json))
    runner.calls = calls
    return runner


# A canned reflection plan for a single sharpened edit, grounded on trace evidence.
def _plan_one_edit(old_id, new_body, *, evidence="ev-u1-0,ev-u1-1",
                   new_title="L (sharpened)"):
    return {"edits": [{
        "verb": "auto_edit",
        "old_id": old_id,
        "new_body": new_body,
        "new_title": new_title,
        "evidence": evidence,
    }]}


# =========================================================================== #
# 1. No-LLM candidate selection (the §5.1 trigger)
# =========================================================================== #

def test_select_picks_mixed_trending_down_agent_unit(tmp_path):
    """A no-LLM trigger: an agent-authored unit with >= MIN_EVIDENCE linked
    outcomes whose outcome_weight is mixed/trending-down (below the edit threshold,
    above hard regression) is SELECTED for the edit track."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="u1", created_by="agent", body="a mediocre lesson")
    # 12 outcomes (>= MIN_EVIDENCE), recent ones negative → trending-down, weight
    # below 1.0 but the net is not a hard regression (mixed, not all-negative).
    _link_outcomes(conn, unit_id="u1", signals=_mixed_trending_down(6, 6))
    # Fold the trace into outcome_weight first (the trigger reads the weight).
    from ultra_memory.maintenance import aggressive_outcomes as ao
    ao.aggregate_unit(conn, "u1", ts=TS)
    cands = aed.select_edit_candidates(conn)
    assert "u1" in {c["unit_id"] for c in cands}


def test_select_skips_sub_evidence_unit(tmp_path):
    """A unit with < MIN_EVIDENCE linked outcomes is NOT selected — too little
    evidence to act on (the conservative floor, fork B)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="u-thin", created_by="agent", body="thinly-evidenced lesson")
    _link_outcomes(conn, unit_id="u-thin", signals=_mixed_trending_down(1, 2))
    from ultra_memory.maintenance import aggressive_outcomes as ao
    ao.aggregate_unit(conn, "u-thin", ts=TS)
    cands = aed.select_edit_candidates(conn)
    assert "u-thin" not in {c["unit_id"] for c in cands}


def test_select_skips_human_and_pinned_units(tmp_path):
    """The selection NEVER reasons over a human / pinned unit — even one with
    abundant evidence (the provenance wall is also respected at SELECT time, not
    only at apply time)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="u-human", created_by="human", body="human rule")
    _save(conn, id="u-pin", created_by="agent", body="pinned lesson", pinned=True)
    for uid in ("u-human", "u-pin"):
        _link_outcomes(conn, unit_id=uid, signals=_mixed_trending_down(6, 6))
    from ultra_memory.maintenance import aggressive_outcomes as ao
    ao.aggregate_unit(conn, "u-human", ts=TS)  # no-ops on a human unit anyway
    cands = aed.select_edit_candidates(conn)
    picked = {c["unit_id"] for c in cands}
    assert "u-human" not in picked
    assert "u-pin" not in picked


def test_select_skips_healthy_high_weight_unit(tmp_path):
    """A unit whose outcomes are strongly positive (high outcome_weight, well above
    the edit threshold) is NOT a "sharpen it" candidate — the loop leaves a healthy
    lesson alone."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="u-good", created_by="agent", body="a strong lesson")
    _link_outcomes(conn, unit_id="u-good", signals=["tests_passed"] * 12)
    from ultra_memory.maintenance import aggressive_outcomes as ao
    ao.aggregate_unit(conn, "u-good", ts=TS)
    cands = aed.select_edit_candidates(conn)
    assert "u-good" not in {c["unit_id"] for c in cands}


# =========================================================================== #
# 2. The trace bundle + reflection prompt (the GEPA-lite core)
# =========================================================================== #

def test_build_trace_bundles_unit_and_outcomes(tmp_path):
    """The trace for a candidate bundles the unit body + its linked outcome events
    (with their outcome_signals) — the GEPA-lite "reflect on the trace" substrate."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="u1", created_by="agent", body="lesson body text")
    _link_outcomes(conn, unit_id="u1", signals=["tests_passed", "tests_failed"])
    trace = aed.build_trace(conn, "u1")
    assert trace["unit_id"] == "u1"
    assert "lesson body text" in trace["body"]
    sigs = {o["outcome_signal"] for o in trace["outcomes"]}
    assert "tests_passed" in sigs and "tests_failed" in sigs


def test_reflection_prompt_demands_targeted_diff_not_free_rewrite(tmp_path):
    """The reflection prompt constrains the model to a TARGETED diff
    (reword/sharpen/merge/correct) that MUST cite trace evidence — NOT a free
    rewrite. The prompt text encodes the constraint (the constraint is ALSO
    enforced in code at plan-parse, but the prompt must ask for it)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="u1", created_by="agent", body="lesson body")
    _link_outcomes(conn, unit_id="u1", signals=["tests_failed", "tests_passed"])
    trace = aed.build_trace(conn, "u1")
    prompt = aed.build_reflection_prompt([trace])
    low = prompt.lower()
    assert "evidence" in low                       # must cite trace evidence
    assert "targeted" in low or "diff" in low      # a targeted diff, not a rewrite
    assert "u1" in prompt                          # the unit is in the prompt


# =========================================================================== #
# 3. Reflect — ONE batched call through the INJECTED runner (no `claude`)
# =========================================================================== #

def test_reflect_makes_one_batched_injected_call(tmp_path):
    """`reflect` issues exactly ONE batched OAuth call through the injected runner
    (never spawns `claude`), returning the parsed plan."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="u1", created_by="agent", body="lesson")
    _link_outcomes(conn, unit_id="u1", signals=["tests_failed"])
    trace = aed.build_trace(conn, "u1")
    runner = _make_runner(_plan_one_edit("u1", "sharpened lesson"))
    plan = aed.reflect([trace], runner=runner, env=FAKE_ENV)
    assert len(runner.calls) == 1                  # exactly ONE batched call
    assert "edits" in plan
    assert plan["edits"][0]["old_id"] == "u1"
    # The injected runner mimics the claude CLI contract — never an API endpoint.
    cmd = runner.calls[0]
    assert "api.anthropic.com" not in " ".join(str(c) for c in cmd)


def test_reflect_drops_ungrounded_diff(tmp_path):
    """An edit with NO trace-evidence citation is DROPPED at plan-parse — an
    ungrounded "improvement" is eval-rejected (spec §5.1: "an un-grounded
    improvement is rejected")."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="u1", created_by="agent", body="lesson")
    trace = aed.build_trace(conn, "u1")
    # The model returns a diff with an EMPTY evidence field → ungrounded.
    bad_plan = {"edits": [{"verb": "auto_edit", "old_id": "u1",
                           "new_body": "rewrite with no grounding", "evidence": ""}]}
    runner = _make_runner(bad_plan)
    plan = aed.reflect([trace], runner=runner, env=FAKE_ENV)
    # The ungrounded edit must not survive into the plan.
    assert plan.get("edits", []) == []


def test_reflect_failopen_on_unparseable_output(tmp_path):
    """A non-JSON / garbled model reply degrades fail-open to an EMPTY plan — the
    aggressive pass never raises out into the maintenance run."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="u1", created_by="agent", body="lesson")
    trace = aed.build_trace(conn, "u1")
    runner = _make_runner("not json at all <<<")
    plan = aed.reflect([trace], runner=runner, env=FAKE_ENV)
    assert plan == {"edits": []} or plan.get("edits", []) == []


def test_reflect_module_no_anthropic_sdk_import():
    """The edit module routes the LLM call through the injected runner / the
    OAuth claude_cli chokepoint — NEVER the anthropic SDK / API."""
    src = Path(aed.__file__).read_text()
    for forbidden in ("import anthropic", "from anthropic", "ANTHROPIC_API_KEY",
                      "messages.create", "cache_control", "api.anthropic.com"):
        assert forbidden not in src, f"OAuth-only violation: {forbidden!r} in edit"


def _env_capturing_runner(captured, plan):
    """A subprocess.run-compatible runner that records the `env=` it receives and
    returns a canned plan. NEVER spawns `claude`."""
    def runner(cmd, **kwargs):
        captured["called"] = True
        captured["env"] = kwargs.get("env")

        class P:
            returncode = 0
            stdout = json.dumps(plan)
            stderr = ""
        return P()
    return runner


def test_reflect_routes_through_oauth_chokepoint_sanitized_env(tmp_path, monkeypatch):
    """FIX #7 (OAuth hardening): the reflection LLM call routes through
    `ultra_memory.claude_cli.run_claude` — the env-sanitizing chokepoint. On the
    happy path (OAuth token present, NO stray metered-API key) the child env handed
    to the runner MUST carry the OAuth token and MUST NOT carry the metered-API key,
    and the recursion markers are stripped. Asserted via the BEHAVIOR (the env the
    injected runner receives), never a source literal."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-real")
    monkeypatch.setenv("CLAUDECODE", "1")          # a recursion marker the chokepoint strips

    captured = {}
    runner = _env_capturing_runner(captured, _plan_one_edit("u1", "sharpened lesson"))

    conn = _open_temp_db(tmp_path)
    _save(conn, id="u1", created_by="agent", body="lesson")
    _link_outcomes(conn, unit_id="u1", signals=["tests_failed"])
    trace = aed.build_trace(conn, "u1")
    # env=None → run_claude sanitizes the AMBIENT os.environ (the real cron-run path).
    aed.reflect([trace], runner=runner)

    assert captured.get("called"), "the reflection call must reach the injected runner"
    child_env = captured["env"]
    assert child_env is not None, "run_claude must pass a sanitized env= to the runner"
    assert "ANTHROPIC_API_KEY" not in child_env, "the metered-API key must be absent (OAuth-only)"
    assert "CLAUDECODE" not in child_env, "the in-session recursion marker must be stripped"
    assert child_env.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok-real", "OAuth token required"


def test_reflect_refuses_when_stray_metered_api_key_present(tmp_path, monkeypatch):
    """FIX #7 (OAuth hardening): a stray non-empty metered-API key in the ambient env
    would, WITHOUT the chokepoint, be inherited by the child `claude` and outrank the
    OAuth token → the metered API. Routed through `run_claude`, the chokepoint REFUSES
    (raises) before any spawn; `reflect` fail-opens to an EMPTY plan and the injected
    runner is NEVER reached — the call cannot leak onto the metered API."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stray")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-real")

    captured = {}
    runner = _env_capturing_runner(captured, _plan_one_edit("u1", "sharpened lesson"))

    conn = _open_temp_db(tmp_path)
    _save(conn, id="u1", created_by="agent", body="lesson")
    _link_outcomes(conn, unit_id="u1", signals=["tests_failed"])
    trace = aed.build_trace(conn, "u1")
    plan = aed.reflect([trace], runner=runner)

    assert not captured.get("called"), "the chokepoint must refuse before reaching the runner"
    assert plan == {"edits": []}, "a refused call fail-opens to an EMPTY plan"


# =========================================================================== #
# 4. Apply — redirect-preserving versioning + superseded_by + bounded + gated
# =========================================================================== #

def test_apply_edit_redirect_preserving_old_recoverable(tmp_path):
    """An admitted trace-reflective diff is applied via redirect-preserving
    versioning: the OLD version survives as a recoverable redirect, the NEW version
    is active (background_review), and a superseded_by link carries the trace
    refs."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="u-old", created_by="agent", body="OLD mediocre lesson")
    admitted = [{"verb": "auto_edit", "old_id": "u-old",
                 "new_body": "SHARPER lesson", "new_title": "L'",
                 "evidence": "ev-u1-0,ev-u1-1"}]
    applied = aed.apply_edits(conn, admitted, ts=TS)
    assert len(applied) == 1
    new_id = applied[0]["new_id"]
    old = _row(conn, "u-old")
    new = _row(conn, new_id)
    # Old preserved verbatim, redirected (recoverable) — NOT deleted.
    assert old["status"] == "redirect"
    assert old["body"] == "OLD mediocre lesson"
    assert old["supersedes"] == new_id
    # New version active + background_review provenance.
    assert new["status"] == "active"
    assert new["body"] == "SHARPER lesson"
    assert new["created_by"] == "background_review"
    # superseded_by edge old -> new carrying the trace refs as evidence.
    link = conn.execute(
        "SELECT predicate, dst_id, evidence FROM links "
        "WHERE src_id='u-old' AND predicate='superseded_by'").fetchone()
    assert link is not None and link["dst_id"] == new_id
    assert "ev-u1-0" in (link["evidence"] or "")


def test_apply_edit_blocks_forbidden_target_halts_run(tmp_path):
    """assert_mutable (re-reading the live row) blocks a forbidden (human) target.
    A single forbidden target HALTS the whole apply — the §4a stop-the-world (zero
    tolerance), NOT a per-item skip: NOTHING in the batch is applied."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="u-agent", created_by="agent", body="agent lesson")
    _save(conn, id="u-human", created_by="human", body="HUMAN rule")
    before = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
    admitted = [
        {"verb": "auto_edit", "old_id": "u-agent", "new_body": "fine", "evidence": "e"},
        {"verb": "auto_edit", "old_id": "u-human", "new_body": "ILLEGAL", "evidence": "e"},
    ]
    with pytest.raises(aw.ForbiddenTargetError):
        aed.apply_edits(conn, admitted, ts=TS)
    # ZERO-tolerance halt — NEITHER edit applied (not even the legal agent one).
    after = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
    assert after == before
    assert _row(conn, "u-agent")["status"] == "active"     # untouched
    assert _row(conn, "u-human")["status"] == "active"     # untouched


def test_apply_edit_respects_max_edits_bound(tmp_path):
    """The apply is bounded to MAX_EDITS_PER_RUN, halt-on-exceed: an admitted set
    larger than the cap applies NONE of the class (not the first N) — the §4c
    stop-and-ask."""
    conn = _open_temp_db(tmp_path)
    from ultra_memory.maintenance.aggressive_bounds import MAX_EDITS_PER_RUN
    n = MAX_EDITS_PER_RUN + 1
    admitted = []
    for i in range(n):
        _save(conn, id=f"u{i}", created_by="agent", body=f"lesson {i}")
        admitted.append({"verb": "auto_edit", "old_id": f"u{i}",
                         "new_body": f"sharper {i}", "evidence": "e"})
    applied = aed.apply_edits(conn, admitted, ts=TS, max_edits=MAX_EDITS_PER_RUN)
    # Halt-on-exceed: NONE applied (not the first MAX_EDITS).
    assert applied == []
    for i in range(n):
        assert _row(conn, f"u{i}")["status"] == "active"   # all untouched


def test_apply_edit_under_bound_applies_all(tmp_path):
    """An admitted set AT/under the cap applies every edit."""
    conn = _open_temp_db(tmp_path)
    from ultra_memory.maintenance.aggressive_bounds import MAX_EDITS_PER_RUN
    n = MAX_EDITS_PER_RUN
    admitted = []
    for i in range(n):
        _save(conn, id=f"u{i}", created_by="agent", body=f"lesson {i}")
        admitted.append({"verb": "auto_edit", "old_id": f"u{i}",
                         "new_body": f"sharper {i}", "evidence": "e"})
    applied = aed.apply_edits(conn, admitted, ts=TS, max_edits=MAX_EDITS_PER_RUN)
    assert len(applied) == n
    for i in range(n):
        assert _row(conn, f"u{i}")["status"] == "redirect"  # all edited


# =========================================================================== #
# 5. Eval-gated end-to-end — a degrading edit is rejected, a clean one applied
# =========================================================================== #

def _seed_probe_corpus(conn):
    _save(conn, id="vix-term", title="VIX spike regime",
          body="When the VIX spikes above thirty volatility regime turns risk-off "
               "and credit spreads widen sharply across the curve.")
    _save(conn, id="theta-term", title="Theta decay accelerates",
          body="Short option premium decays fastest in the final week before "
               "expiration as theta accelerates into the gamma zone.")


def _probes():
    return [
        {"query": "VIX spike volatility regime risk-off", "expect": "vix-term"},
        {"query": "theta decay short premium expiration gamma", "expect": "theta-term"},
    ]


def test_run_edit_track_rejects_degrading_applies_clean(tmp_path):
    """End-to-end (with the eval gate): a probe-REGRESSING edit (guts the
    distinctive terms) is rejected and NOT applied; a clean sharpening edit clears
    the gate and IS applied. The eval gate is the pre-commit defense."""
    conn = _open_temp_db(tmp_path)
    _seed_probe_corpus(conn)
    # The model proposes one clean sharpen (theta-term keeps its terms) + one
    # gutting rewrite (vix-term stripped of its distinctive terms).
    plan = {"edits": [
        {"verb": "auto_edit", "old_id": "theta-term",
         "new_title": "Theta decay accelerates",
         "new_body": "Short option premium decays fastest in the final week before "
                     "expiration as theta accelerates into the gamma zone. Note: "
                     "monitor weekend decay.",
         "evidence": "ev:theta"},
        {"verb": "auto_edit", "old_id": "vix-term",
         # A genuine gutting: strip the distinctive terms from BOTH title AND body
         # (else the preserved title alone keeps the unit retrievable — see the
         # eval module, which preserves title on the shadow version).
         "new_title": "Generic placeholder note",
         "new_body": "generic placeholder text with none of the distinctive terms",
         "evidence": "ev:vix"},
    ]}
    result = aed.run_edit_track(
        conn, plan, probes=_probes(), embedder=_stub_embedder, ts=TS)
    applied_old = {a["old_id"] for a in result["applied"]}
    assert "theta-term" in applied_old              # clean edit applied
    assert "vix-term" not in applied_old            # degrading edit rejected
    # theta-term redirected to a new version; vix-term untouched (still active).
    assert _row(conn, "theta-term")["status"] == "redirect"
    assert _row(conn, "vix-term")["status"] == "active"
    assert _body(conn, "vix-term").startswith("When the VIX")  # un-gutted


def test_run_edit_track_halts_on_forbidden_target(tmp_path):
    """End-to-end: a plan that targets a human unit fails the eval HARD gate →
    the whole run halts → NOTHING applied (not even the legal edits)."""
    conn = _open_temp_db(tmp_path)
    _seed_probe_corpus(conn)
    _save(conn, id="u-human", created_by="human", body="human rule")
    plan = {"edits": [
        {"verb": "auto_edit", "old_id": "theta-term",
         "new_body": _body(conn, "theta-term") + " clean tail", "evidence": "e"},
        {"verb": "auto_edit", "old_id": "u-human",
         "new_body": "ILLEGAL human edit", "evidence": "e"},
    ]}
    result = aed.run_edit_track(
        conn, plan, probes=_probes(), embedder=_stub_embedder, ts=TS)
    assert result["halt"] is True
    assert result["applied"] == []
    # Even the legal theta-term edit is NOT applied (zero-tolerance stop-the-world).
    assert _row(conn, "theta-term")["status"] == "active"


def test_run_edit_track_failopen_never_raises(tmp_path):
    """Fail-open: a malformed plan degrades to a no-op result, never raises out into
    the maintenance run."""
    conn = _open_temp_db(tmp_path)
    _seed_probe_corpus(conn)
    result = aed.run_edit_track(
        conn, {"edits": "not-a-list"}, probes=_probes(),
        embedder=_stub_embedder, ts=TS)
    assert isinstance(result, dict)
    assert result["applied"] == []


def test_run_edit_track_is_dryrun_safe(tmp_path):
    """In dry-run mode the track PLANS + EVALS but applies NOTHING — the gate
    before any aggressive write lands (spec §4f / §7 step 8)."""
    conn = _open_temp_db(tmp_path)
    _seed_probe_corpus(conn)
    before = _body(conn, "theta-term")
    plan = {"edits": [
        {"verb": "auto_edit", "old_id": "theta-term",
         "new_body": before + " sharpening tail keeping all terms", "evidence": "e"},
    ]}
    result = aed.run_edit_track(
        conn, plan, probes=_probes(), embedder=_stub_embedder, ts=TS, apply=False)
    # The edit cleared the gates (it is admitted) but was NOT applied (dry-run).
    assert any(a["old_id"] == "theta-term" for a in result["admitted"])
    assert result["applied"] == []
    assert _row(conn, "theta-term")["status"] == "active"   # untouched
    assert _body(conn, "theta-term") == before


# --------------------------------------------------------------------------- #
# Archive-never-delete: no destructive call anywhere in the module
# --------------------------------------------------------------------------- #

def test_edit_module_never_deletes():
    """Static guard: the edit module never `rm`s / deletes — every verb is a
    reversible FSM transition / redirect-stub (archive-never-delete)."""
    src = Path(aed.__file__).read_text()
    for forbidden in ("os.remove(", "shutil.rmtree(", ".unlink(",
                      "memory_lib.delete(", ".delete(tier", "rm -rf", "DROP TABLE"):
        assert forbidden not in src, f"destructive call {forbidden!r} in edit"
