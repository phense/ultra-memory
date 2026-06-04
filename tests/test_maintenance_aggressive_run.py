"""Tests for aggressive_run.py — SP-7 §4e/§5.5 + §7 step 8 — the Stage-2c WIRING +
DIGEST + DRY-RUN-FIRST orchestrator. The LAST build stage: it composes the three
aggressive tracks (edit / revert / quarantine) behind the gate (kill switch +
dry-run, aggressive_bounds.run_gate), the pre-run checkpoint (§4d), the bounds
(§4c), and renders the §4e human digest (briefings/YYYY/sp7-self-improvement-*.md)
+ the audit jsonl + the EXACT one-command rollback.

SHIPS DISABLED (the whole point of stage 8):
  * SP7_AGGRESSIVE_DISABLE present → the run is a NO-OP + one log line, applies
    NOTHING, writes no digest (the gate short-circuits before any plan);
  * SP7_AGGRESSIVE_DRYRUN present → the run PLANS + EVALS + DIGESTS but applies
    NOTHING (the human-in-the-audit-loop gate before any aggressive write lands);
  * neither set → LIVE: plan + eval + digest + bounded apply within the wall.

The cron path keeps SP7_AGGRESSIVE_DISABLE=1 (on-demand-first / cron-disabled,
fork C) — a SHELL-level concern; this module's job is to honor the gate the env
sets. A dedicated test asserts the gate short-circuits on DISABLE.

DIGEST (spec §4e) under test: it renders per-skill outcome_weight rates, the
edits proposed/applied/eval-rejected, the proposed reversions (for the operator), the
quarantine pairs (for the operator's adjudication), any bound-hit, and the EXACT
rollback command (git reset + memory_import).

OAUTH-ONLY (HARD): every LLM call routes through the tracks' INJECTED runner;
tests inject a fake runner + stub embedder and NEVER spawn `claude`, NEVER load
fastembed. A guard test asserts no anthropic SDK / API import in this module.

ARCHIVE-NEVER-DELETE / FAIL-OPEN: a dirty tree skips the pass (the checkpoint
§4d clean-tree precondition); any error degrades to a no-op + one line, never
wedges the maintenance run.

These tests NEVER touch the live memory.db, NEVER spawn `claude`, NEVER load a
real embedder, and NEVER tag the real repo — they run against a temp DB + a temp
git repo + synthetic agent-authored memories + a frozen retrieval probe set + an
injected runner.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


from ultra_memory.maintenance import aggressive_run as ar  # noqa: E402
from ultra_memory.maintenance import aggressive_bounds as ab  # noqa: E402
from ultra_memory.maintenance import aggressive_outcomes as ao  # noqa: E402
from ultra_memory import memory_lib  # noqa: E402


TS = "2026-05-31T00:00:00Z"
DATE = "2026-05-31"

# A valid fake OAuth env so run_claude's _child_env() does not raise OAuthViolation
# (the tracks' LLM calls now route through the ultra_memory.claude_cli chokepoint,
# which requires CLAUDE_CODE_OAUTH_TOKEN + refuses ANTHROPIC_API_KEY). Tests that
# exercise a track's LLM call thread this as `oauth_env=` so the injected runner is
# actually reached; no `claude` is ever spawned.
FAKE_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "tok-test"}


# --------------------------------------------------------------------------- #
# Fixture helpers — a temp DB + a temp git repo + synthetic agent-authored
# memories + outcome signals. NO live store, NO claude, NO fastembed.
# --------------------------------------------------------------------------- #

def _open_temp_db(tmp_path, name="memory.db"):
    return memory_lib.open_memory_db(str(tmp_path / name))


def _save(conn, *, id, created_by="agent", body="a lesson", title="L",
          pinned=False, type="learning", index_hook=None):
    memory_lib.save_memory(
        conn, id=id, type=type, title=title, body=body, ts=TS,
        created_by=created_by, index_hook=index_hook)
    if pinned:
        memory_lib.set_pinned(conn, id=id, pinned=True, ts=TS, reason="test pin")
    return id


def _event(conn, *, session_id, outcome_signal, ts=TS, title="ev", detail="d"):
    memory_lib.record_session_event(
        conn, session_id=session_id, kind="skill_learning_candidate",
        title=title, ts=ts, detail=detail, outcome_signal=outcome_signal)
    row = conn.execute(
        "SELECT id FROM session_events ORDER BY id DESC LIMIT 1").fetchone()
    return int(row["id"])


def _link_outcomes(conn, *, unit_id, signals):
    for i, sig in enumerate(signals):
        ev = _event(conn, session_id=f"s-{unit_id}-{i}", outcome_signal=sig,
                    title=f"ev-{unit_id}-{i}")
        memory_lib.record_link(
            conn, src_kind="session_event", src_id=str(ev),
            predicate="validated_as", dst_kind="memory", dst_id=unit_id, ts=TS)


def _stub_embedder(texts):
    # A trivial deterministic embedder — never fastembed, never network. Distinct
    # constant vectors so nothing accidentally lands "near" unless intended.
    vocab = ("vix", "spike", "sell", "buy", "premium", "macd", "tax")
    return [[float((t or "").lower().count(tok)) for tok in vocab] for t in texts]


def _quiet_runner(stdout="{}"):
    """An injected runner that returns an empty/parseable reply — NEVER spawns
    `claude`. Default produces no actionable plan (the tracks fail-open to empty)."""
    def runner(cmd, capture_output=True, text=True, timeout=None, env=None):
        class P:
            returncode = 0
        P.stdout = stdout
        P.stderr = ""
        return P
    return runner


def _init_git_repo(tmp_path):
    """A throwaway git repo with one committed file so a tag has something to point
    at. Returns the repo root (Path). Clean tree by default."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True, env=env)
    return repo, env


def _probes():
    """A small frozen retrieval probe set (the §6 quality-gate fixture shape).
    Empty queries → the eval's quality gate is a trivial pass (no edits proposed in
    these fixtures), which is all the wiring test needs."""
    return []


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test starts with BOTH kill-switch envs unset (LIVE by default); tests
    set them explicitly. Prevents an ambient SP7_AGGRESSIVE_* from leaking in."""
    monkeypatch.delenv(ab._DISABLE_ENV, raising=False)
    monkeypatch.delenv(ab._DRYRUN_ENV, raising=False)
    yield


# =========================================================================== #
# 1. The kill switch — DISABLE short-circuits to a no-op (SHIPS DISABLED).
# =========================================================================== #

def test_disable_env_is_a_noop(tmp_path, monkeypatch):
    """SP7_AGGRESSIVE_DISABLE present → the whole pass is a no-op: mode='noop',
    nothing applied, NO digest written, NO checkpoint attempted. This is the
    'ships disabled' invariant — the cron env keeps this set."""
    monkeypatch.setenv(ab._DISABLE_ENV, "1")
    conn = _open_temp_db(tmp_path)
    repo, env = _init_git_repo(tmp_path)
    logs = []
    res = ar.run_aggressive_pass(
        conn, repo_root=repo, date=DATE, ts=TS, probes=_probes(),
        embedder=_stub_embedder, runner=_quiet_runner(), git_env=env,
        export_fn=lambda: None, log=logs.append)
    assert res.mode == "noop"
    assert res.applied_counts == {"edits": 0, "reversions": 0, "quarantines": 0}
    assert res.digest_path is None          # disabled writes no digest
    assert res.checkpoint is None           # disabled does not even checkpoint
    # exactly one log line about being disabled.
    assert any("DISABLED" in m for m in logs)


def test_disable_takes_precedence_over_dryrun(tmp_path, monkeypatch):
    """DISABLE outranks DRYRUN — a disabled pass does not even plan/digest."""
    monkeypatch.setenv(ab._DISABLE_ENV, "1")
    monkeypatch.setenv(ab._DRYRUN_ENV, "1")
    conn = _open_temp_db(tmp_path)
    repo, env = _init_git_repo(tmp_path)
    res = ar.run_aggressive_pass(
        conn, repo_root=repo, date=DATE, ts=TS, probes=_probes(),
        embedder=_stub_embedder, runner=_quiet_runner(), git_env=env,
        export_fn=lambda: None)
    assert res.mode == "noop"
    assert res.digest_path is None


# =========================================================================== #
# 2. Dry-run — plans + digests but applies NOTHING.
# =========================================================================== #

def test_dryrun_writes_digest_applies_nothing(tmp_path, monkeypatch):
    """SP7_AGGRESSIVE_DRYRUN present → mode='dryrun': it runs the plan + eval +
    DIGEST but applies NOTHING (the human-in-the-audit-loop gate). The digest file
    is written under briefings/YYYY/sp7-self-improvement-YYYY-MM-DD.md."""
    monkeypatch.setenv(ab._DRYRUN_ENV, "1")
    conn = _open_temp_db(tmp_path)
    # A regressed graduation so the revert track has a PROPOSED reversion to digest.
    _save(conn, id="bad", body="a graduated lesson that hurts", index_hook="risk-manager")
    _link_outcomes(conn, unit_id="bad",
                   signals=["tests_failed"] * 10 + ["trade_loss"] * 2)
    repo, env = _init_git_repo(tmp_path)
    res = ar.run_aggressive_pass(
        conn, repo_root=repo, date=DATE, ts=TS, probes=_probes(),
        embedder=_stub_embedder, runner=_quiet_runner(),
        briefings_dir=tmp_path / "briefings", git_env=env,
        export_fn=lambda: None, include_graduations=True)
    assert res.mode == "dryrun"
    # Applied NOTHING in dry-run.
    assert res.applied_counts == {"edits": 0, "reversions": 0, "quarantines": 0}
    # The regressed unit is STILL active (nothing reverted).
    row = conn.execute("SELECT status FROM memories WHERE id='bad'").fetchone()
    assert row["status"] == "active"

    # FIX 5: a dry-run must apply NOTHING — including the §5.2 outcome_weight
    # aggregate. The net-negative 'bad' unit's outcome_weight must be UNCHANGED on
    # disk (still the inert 1.0 default); aggregate_all must NOT commit a demotion
    # in dry-run (that would silently degrade recall rank — a real side effect).
    weight_on_disk = conn.execute(
        "SELECT outcome_weight FROM memories WHERE id='bad'").fetchone()["outcome_weight"]
    assert weight_on_disk == 1.0, (
        f"dry-run must NOT persist an outcome_weight demotion; got {weight_on_disk}")

    # ...but the digest STILL reports the per-skill rate (the would-be weight is
    # computed in-memory for the digest, just not persisted).
    assert "risk-manager" in res.per_skill_rates
    assert res.per_skill_rates["risk-manager"] < 1.0, (
        "the would-be (in-memory) per-skill rate must reflect the net-negative outcomes")

    # The digest WAS written and names the proposed reversion.
    assert res.digest_path is not None
    digest = Path(res.digest_path).read_text()
    assert "bad" in digest
    assert res.proposed_reversions  # the propose-for-the-operator payload is populated


def test_dryrun_does_not_tag_the_repo(tmp_path, monkeypatch):
    """Dry-run does NOT create the pre-run tag (no aggressive write → no
    checkpoint needed). The tag is a LIVE-apply anchor only."""
    monkeypatch.setenv(ab._DRYRUN_ENV, "1")
    conn = _open_temp_db(tmp_path)
    repo, env = _init_git_repo(tmp_path)
    ar.run_aggressive_pass(
        conn, repo_root=repo, date=DATE, ts=TS, probes=_probes(),
        embedder=_stub_embedder, runner=_quiet_runner(),
        briefings_dir=tmp_path / "briefings", git_env=env, export_fn=lambda: None)
    tags = subprocess.run(["git", "tag"], cwd=repo, capture_output=True,
                          text=True, env=env).stdout.split()
    assert f"pre-sp7-aggressive-{DATE}" not in tags


# =========================================================================== #
# 3. The digest renderer (spec §4e) — counts, per-skill rates, rollback command.
# =========================================================================== #

def test_render_digest_contains_all_sections():
    """The §4e digest renders: per-skill outcome_weight rates, edits
    proposed/applied/eval-rejected, the proposed reversions, the quarantine pairs,
    a bound-hit, and the EXACT rollback command."""
    rr = ar.RunResult(
        mode="dryrun", date=DATE,
        per_skill_rates={"risk-manager": 0.45, "backtest": 1.10},
        edits_proposed=[{"unit_id": "u1"}, {"unit_id": "u2"}],
        edits_applied=[],
        edits_rejected=[{"unit_id": "u2", "reason": "probe_regression"}],
        proposed_reversions=[{"regressed_id": "n1", "prior_id": "o1"}],
        quarantine_pairs=[{"id_a": "qa", "id_b": "qb"}],
        bounds_hit=[{"cls": "edits", "proposed": 7, "cap": 3, "scope": "run"}],
        rollback_command="cd /repo && git reset --soft pre-sp7-aggressive-2026-05-31",
        applied_counts={"edits": 0, "reversions": 0, "quarantines": 0},
    )
    md = ar.render_digest(rr)
    # per-skill rates.
    assert "risk-manager" in md and "0.45" in md
    assert "backtest" in md and "1.10" in md
    # edits proposed / applied / rejected counts.
    assert "proposed" in md.lower()
    assert "rejected" in md.lower() or "eval-rejected" in md.lower()
    assert "probe_regression" in md
    # proposed reversions (for the operator).
    assert "n1" in md and "o1" in md
    # quarantine pairs (for the operator).
    assert "qa" in md and "qb" in md
    # bound-hit surfaced.
    assert "bound" in md.lower() and "cap=3" in md
    # the EXACT rollback command.
    assert "git reset --soft pre-sp7-aggressive-2026-05-31" in md


def test_render_digest_dryrun_banner():
    """A dry-run digest is clearly marked DRY-RUN / APPLIED NOTHING so the operator reads
    it as a proposal, not a record of writes."""
    rr = ar.RunResult(mode="dryrun", date=DATE,
                      applied_counts={"edits": 0, "reversions": 0, "quarantines": 0})
    md = ar.render_digest(rr)
    assert "DRY-RUN" in md.upper()


def test_render_digest_live_banner():
    rr = ar.RunResult(mode="live", date=DATE,
                      applied_counts={"edits": 1, "reversions": 0, "quarantines": 2})
    md = ar.render_digest(rr)
    assert "LIVE" in md.upper()
    # applied counts surfaced.
    assert "1" in md and "2" in md


def test_digest_path_shape(tmp_path):
    """The digest path is briefings/<YYYY>/sp7-self-improvement-<YYYY-MM-DD>.md."""
    p = ar.digest_path_for(tmp_path / "briefings", DATE)
    assert p.name == f"sp7-self-improvement-{DATE}.md"
    assert p.parent.name == "2026"


# =========================================================================== #
# 4. Per-skill outcome_weight rates (the digest's first section).
# =========================================================================== #

def test_per_skill_rates_aggregate_by_index_hook(tmp_path):
    """Per-skill rates group agent-authored units by their index_hook (the skill
    tag) and average their outcome_weight."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="r1", index_hook="risk-manager")
    _save(conn, id="r2", index_hook="risk-manager")
    _save(conn, id="b1", index_hook="backtest")
    memory_lib.set_outcome_weight(conn, id="r1", weight=0.4, ts=TS, reason="t")
    memory_lib.set_outcome_weight(conn, id="r2", weight=0.6, ts=TS, reason="t")
    memory_lib.set_outcome_weight(conn, id="b1", weight=1.2, ts=TS, reason="t")
    rates = ar.per_skill_outcome_rates(conn)
    assert rates["risk-manager"] == pytest.approx(0.5)
    assert rates["backtest"] == pytest.approx(1.2)


def test_per_skill_rates_failopen_empty(tmp_path):
    """No agent-authored units → empty rates, no error."""
    conn = _open_temp_db(tmp_path)
    assert ar.per_skill_outcome_rates(conn) == {}


# =========================================================================== #
# 5. LIVE apply — bounded, gated, reversible (composes the wall + bounds).
# =========================================================================== #

def test_live_applies_quarantine_within_bound(tmp_path, monkeypatch):
    """LIVE (no env set): an opposing agent-authored pair the adjudicator labels
    contradicts gets BOTH quarantined (out of recall), within the bound, after the
    pre-run checkpoint. The digest is written; the rollback command is exact."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="qa", body="On a VIX spike SELL premium", title="A",
          index_hook="risk-manager")
    _save(conn, id="qb", body="On a VIX spike BUY premium", title="B",
          created_by="background_review", index_hook="risk-manager")

    # A runner that labels the (qa, qb) pair as contradicting.
    def runner(cmd, capture_output=True, text=True, timeout=None, env=None):
        class P:
            returncode = 0
            stderr = ""
        P.stdout = json.dumps({"adjudications": [
            {"id_a": "qa", "id_b": "qb", "label": "contradicts"}]})
        return P

    # An embedder that lands qa/qb NEAR (shared 'vix'/'spike'/'premium' tokens).
    repo, env = _init_git_repo(tmp_path)
    res = ar.run_aggressive_pass(
        conn, repo_root=repo, date=DATE, ts=TS, probes=_probes(),
        embedder=_stub_embedder, runner=runner, oauth_env=FAKE_ENV,
        briefings_dir=tmp_path / "briefings", git_env=env, export_fn=lambda: None)
    assert res.mode == "live"
    # BOTH quarantined.
    for mid in ("qa", "qb"):
        row = conn.execute("SELECT status FROM memories WHERE id=?", (mid,)).fetchone()
        assert row["status"] == "quarantined"
    assert res.applied_counts["quarantines"] == 1   # one PAIR
    # the pre-run tag exists (checkpoint made before the write).
    tags = subprocess.run(["git", "tag"], cwd=repo, capture_output=True,
                          text=True, env=env).stdout.split()
    assert f"pre-sp7-aggressive-{DATE}" in tags
    # the digest exists + carries the rollback command.
    digest = Path(res.digest_path).read_text()
    assert f"pre-sp7-aggressive-{DATE}" in digest


def test_live_dirty_tree_skips_apply(tmp_path, monkeypatch):
    """A DIRTY working tree (the 2026-05-24 untracked-files gap) → the checkpoint
    refuses → the LIVE pass applies NOTHING (fail-soft skip), still writes a digest
    explaining the skip. This is the §4d recoverability precondition."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="qa", body="On a VIX spike SELL premium", title="A")
    _save(conn, id="qb", body="On a VIX spike BUY premium", title="B",
          created_by="background_review")

    def runner(cmd, capture_output=True, text=True, timeout=None, env=None):
        class P:
            returncode = 0
            stderr = ""
        P.stdout = json.dumps({"adjudications": [
            {"id_a": "qa", "id_b": "qb", "label": "contradicts"}]})
        return P

    repo, env = _init_git_repo(tmp_path)
    (repo / "untracked.txt").write_text("dirty\n")     # makes the tree dirty
    res = ar.run_aggressive_pass(
        conn, repo_root=repo, date=DATE, ts=TS, probes=_probes(),
        embedder=_stub_embedder, runner=runner, oauth_env=FAKE_ENV,
        briefings_dir=tmp_path / "briefings", git_env=env, export_fn=lambda: None)
    # NOTHING applied — the units stay active.
    for mid in ("qa", "qb"):
        row = conn.execute("SELECT status FROM memories WHERE id=?", (mid,)).fetchone()
        assert row["status"] == "active"
    assert res.applied_counts == {"edits": 0, "reversions": 0, "quarantines": 0}
    assert res.checkpoint is not None and res.checkpoint.ok is False
    # the digest still exists and explains the skip.
    assert res.digest_path is not None
    digest = Path(res.digest_path).read_text()
    assert "dirty" in digest.lower() or "skip" in digest.lower()


def test_live_forbidden_target_halts_the_run(tmp_path, monkeypatch):
    """The §4a stop-the-world at the ORCHESTRATOR level: when a track's apply path
    raises ForbiddenTargetError (a forbidden human/pinned target reached the write —
    the wall's re-read-the-live-row guard fired), the orchestrator turns it into a
    whole-run HALT: nothing applied (zero tolerance, NOT a per-item skip), and the
    digest records the halt.

    (The tracks' own SELECT pre-filters already exclude human/pinned rows so the
    apply path is rarely reached in practice — defense-in-depth; this test exercises
    the orchestrator's contract for when it IS reached: a bug / a prompt-injection.)
    """
    conn = _open_temp_db(tmp_path)
    _save(conn, id="qa", body="On a VIX spike SELL premium", title="A")
    _save(conn, id="qb", body="On a VIX spike BUY premium", title="B",
          created_by="background_review")

    # Force the quarantine track's apply path to raise the stop-the-world, as the
    # wall would on a forbidden target re-read.
    from ultra_memory.maintenance.aggressive_wall import ForbiddenTargetError

    def _exploding_quarantine(*a, **k):
        exc = ForbiddenTargetError("forbidden target qb (human/pinned)")
        exc.targets = ["memory:qb"]
        raise exc

    monkeypatch.setattr(ar.aq, "run_quarantine_track", _exploding_quarantine)

    repo, env = _init_git_repo(tmp_path)
    res = ar.run_aggressive_pass(
        conn, repo_root=repo, date=DATE, ts=TS, probes=_probes(),
        embedder=_stub_embedder, runner=_quiet_runner(),
        briefings_dir=tmp_path / "briefings", git_env=env, export_fn=lambda: None)
    # The run HALTED — nothing applied.
    assert res.halt is True
    assert res.applied_counts == {"edits": 0, "reversions": 0, "quarantines": 0}
    assert res.forbidden_targets  # the offending target is recorded for the digest
    # the digest records the halt.
    digest = Path(res.digest_path).read_text()
    assert "halt" in digest.lower() or "forbidden" in digest.lower()


# =========================================================================== #
# 5b. Cross-track halt accuracy (DEFECT 1) — a halt AFTER an earlier track wrote
#     must report what actually landed; the digest must not lie "NOTHING applied".
# =========================================================================== #

def test_halt_after_earlier_track_applied_reports_accurately(tmp_path, monkeypatch):
    """The §4a stop-the-world contract is 'the HALTING action applied nothing' —
    NOT 'nothing in the whole run was applied' when an earlier track already
    committed writes (engine verbs auto-commit per verb; there is no enclosing
    txn). When the edit track applies an edit in a LIVE run and then a LATER track
    (quarantine) raises the stop-the-world, the run is halted but the already-
    landed edit MUST be reported (applied_counts.edits >= 1) and the digest MUST
    NOT claim 'NOTHING was applied'.
    """
    conn = _open_temp_db(tmp_path)
    # An agent-authored unit the edit track will actually edit. It must be a
    # mixed/trending-down candidate (outcome_weight < 1.0, >= MIN_EVIDENCE outcomes)
    # but NOT a HARD regression (net-negative is the reversion track's domain, so it
    # would be excluded from the edit track). NET-POSITIVE outcomes (6 pass / 5 fail
    # = +1) keep it out of is_regression; the manually-seeded sub-1.0 weight makes it
    # a "sharpen it" edit candidate.
    _save(conn, id="ed", body="a mediocre lesson to sharpen", title="E",
          index_hook="backtest")
    _link_outcomes(conn, unit_id="ed",
                   signals=["tests_passed"] * 6 + ["tests_failed"] * 5)
    memory_lib.set_outcome_weight(conn, id="ed", weight=0.4, ts=TS, reason="seed")

    # A reflection runner that returns a grounded targeted diff for "ed".
    def runner(cmd, capture_output=True, text=True, timeout=None, env=None):
        class P:
            returncode = 0
            stderr = ""
        P.stdout = json.dumps({"edits": [
            {"old_id": "ed", "new_title": "E2",
             "new_body": "a sharper lesson", "evidence": "ev-ed-0"}]})
        return P

    # Force the LATER quarantine track to raise the stop-the-world AFTER the edit
    # track has already applied (mimicking a forbidden target reached post-edit).
    from ultra_memory.maintenance.aggressive_wall import ForbiddenTargetError

    def _exploding_quarantine(*a, **k):
        exc = ForbiddenTargetError("forbidden target (human/pinned)")
        exc.targets = ["memory:zz"]
        raise exc

    monkeypatch.setattr(ar.aq, "run_quarantine_track", _exploding_quarantine)

    # A real probe set whose expected hit ('ed') survives the sharpening edit (the
    # word 'lesson' is kept), so the strict quality gate ADMITS the edit. (An empty
    # probe set would now FAIL-CLOSED per FIX 4 and the edit would be held — but
    # this test is about cross-track halt accuracy, not the empty-probe path, so it
    # supplies a probe so the edit can legitimately land.)
    probes = [{"query": "lesson", "expect": "ed"}]
    repo, env = _init_git_repo(tmp_path)
    res = ar.run_aggressive_pass(
        conn, repo_root=repo, date=DATE, ts=TS, probes=probes,
        embedder=_stub_embedder, runner=runner, oauth_env=FAKE_ENV,
        briefings_dir=tmp_path / "briefings", git_env=env, export_fn=lambda: None)

    # The run halted.
    assert res.halt is True
    # The edit ACTUALLY landed before the halt — the OLD unit is now a redirect.
    row = conn.execute("SELECT status FROM memories WHERE id='ed'").fetchone()
    assert row["status"] == "redirect"
    # The applied count must REFLECT the landed edit (not be zeroed by the halt arm).
    assert res.applied_counts["edits"] >= 1
    # The digest must NOT claim nothing was applied — it must surface the landed edit.
    digest = Path(res.digest_path).read_text()
    assert "NOTHING was applied" not in digest
    # It DOES still surface the halt + the partial-apply caveat for recoverability.
    assert "halt" in digest.lower() or "HALTED" in digest
    assert "pre-sp7-aggressive" in digest        # the rollback anchor is shown


def test_halt_before_any_apply_still_says_nothing_applied(tmp_path, monkeypatch):
    """The contract still holds the other way: when the FIRST track halts (no
    earlier track wrote), the digest accurately reports nothing was applied."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="qa", body="On a VIX spike SELL premium", title="A")
    _save(conn, id="qb", body="On a VIX spike BUY premium", title="B",
          created_by="background_review")

    # Make the FIRST track (edit) raise the stop-the-world — nothing applied before.
    from ultra_memory.maintenance.aggressive_wall import ForbiddenTargetError

    def _exploding_edit(*a, **k):
        exc = ForbiddenTargetError("forbidden target (human/pinned)")
        exc.targets = ["memory:qb"]
        raise exc

    monkeypatch.setattr(ar.aedit, "run_edit_track", _exploding_edit)

    repo, env = _init_git_repo(tmp_path)
    res = ar.run_aggressive_pass(
        conn, repo_root=repo, date=DATE, ts=TS, probes=_probes(),
        embedder=_stub_embedder, runner=_quiet_runner(),
        briefings_dir=tmp_path / "briefings", git_env=env, export_fn=lambda: None)
    assert res.halt is True
    assert res.applied_counts == {"edits": 0, "reversions": 0, "quarantines": 0}
    digest = Path(res.digest_path).read_text()
    assert "NOTHING was applied" in digest


# =========================================================================== #
# 5c. Period cap on quarantines (DEFECT 2) — the §4c global per-period aggregate
#     cap must gate quarantines too, not only edits.
# =========================================================================== #

def test_period_cap_blocks_quarantines_across_runs(tmp_path, monkeypatch):
    """The §4c period cap ('stacked re-runs cannot accumulate past the budget')
    must cover QUARANTINES, not only edits. With the period quarantine budget
    already exhausted (seeded in `meta`), a LIVE run that the adjudicator would
    quarantine applies NONE — the period bound halts the class and surfaces a
    bound-hit. (Pre-defect, only the per-run cap applied here.)"""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="qa", body="On a VIX spike SELL premium", title="A",
          index_hook="risk-manager")
    _save(conn, id="qb", body="On a VIX spike BUY premium", title="B",
          created_by="background_review", index_hook="risk-manager")

    # Pre-seed the per-period quarantine counter AT the period cap so this run is
    # over-budget for the period.
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (ab._period_key(DATE[:7], "quarantines"), str(ar._PERIOD_CAP_QUARANTINES)))
    conn.commit()

    def runner(cmd, capture_output=True, text=True, timeout=None, env=None):
        class P:
            returncode = 0
            stderr = ""
        P.stdout = json.dumps({"adjudications": [
            {"id_a": "qa", "id_b": "qb", "label": "contradicts"}]})
        return P

    repo, env = _init_git_repo(tmp_path)
    res = ar.run_aggressive_pass(
        conn, repo_root=repo, date=DATE, ts=TS, probes=_probes(),
        embedder=_stub_embedder, runner=runner, oauth_env=FAKE_ENV,
        briefings_dir=tmp_path / "briefings", git_env=env, export_fn=lambda: None)
    assert res.mode == "live"
    # NOTHING quarantined — the period cap halted the class.
    assert res.applied_counts["quarantines"] == 0
    for mid in ("qa", "qb"):
        row = conn.execute("SELECT status FROM memories WHERE id=?", (mid,)).fetchone()
        assert row["status"] == "active"
    # A period-scope bound-hit on quarantines is surfaced in the digest.
    assert any(b.get("cls") == "quarantines" and b.get("scope") == "period"
               for b in res.bounds_hit)


def test_period_cap_allows_quarantines_under_budget(tmp_path, monkeypatch):
    """Sanity guard for DEFECT 2's fix: with the period quarantine budget NOT
    exhausted, a LIVE quarantine still applies (the period gate is additive, not a
    blanket block)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="qa", body="On a VIX spike SELL premium", title="A")
    _save(conn, id="qb", body="On a VIX spike BUY premium", title="B",
          created_by="background_review")

    def runner(cmd, capture_output=True, text=True, timeout=None, env=None):
        class P:
            returncode = 0
            stderr = ""
        P.stdout = json.dumps({"adjudications": [
            {"id_a": "qa", "id_b": "qb", "label": "contradicts"}]})
        return P

    repo, env = _init_git_repo(tmp_path)
    res = ar.run_aggressive_pass(
        conn, repo_root=repo, date=DATE, ts=TS, probes=_probes(),
        embedder=_stub_embedder, runner=runner, oauth_env=FAKE_ENV,
        briefings_dir=tmp_path / "briefings", git_env=env, export_fn=lambda: None)
    assert res.applied_counts["quarantines"] == 1
    # the period counter is now incremented to 1.
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?",
        (ab._period_key(DATE[:7], "quarantines"),)).fetchone()
    assert int(row[0]) == 1


# =========================================================================== #
# 5d. Evidence-visibility in the digest (DEFECT 4) — the dry-run must not be
#     misread as 'the loop sees nothing wrong' when it has near-zero evidence.
# =========================================================================== #

def test_digest_surfaces_outcome_evidence_coverage(tmp_path, monkeypatch):
    """The digest must make the loop's EVIDENCE coverage explicit so a near-empty
    dry-run is not misread as a clean bill of health. With agent-authored units
    that have FEWER than MIN_EVIDENCE linked outcomes, the digest reports the
    sub-floor evidence state (so an operator knows the loop had near-zero evidence
    to reason over, per the SP-6 outcome-attribution-backfill dependency)."""
    monkeypatch.setenv(ab._DRYRUN_ENV, "1")
    conn = _open_temp_db(tmp_path)
    # A unit with only ONE linked outcome — far below MIN_EVIDENCE=10.
    _save(conn, id="thin", body="a lesson with thin evidence", index_hook="backtest")
    _link_outcomes(conn, unit_id="thin", signals=["tests_passed"])
    repo, env = _init_git_repo(tmp_path)
    res = ar.run_aggressive_pass(
        conn, repo_root=repo, date=DATE, ts=TS, probes=_probes(),
        embedder=_stub_embedder, runner=_quiet_runner(),
        briefings_dir=tmp_path / "briefings", git_env=env, export_fn=lambda: None)
    # The RunResult carries the evidence coverage.
    assert res.evidence_coverage is not None
    assert res.evidence_coverage["below_floor"] >= 1
    assert res.evidence_coverage["min_evidence"] == 10
    digest = Path(res.digest_path).read_text()
    # The digest names the evidence coverage + the sub-floor caveat.
    assert "evidence" in digest.lower()
    assert "MIN_EVIDENCE" in digest or "below" in digest.lower()


def test_render_digest_evidence_section_present():
    """The §4e digest always renders an evidence-coverage section so the reader
    can calibrate how much the loop actually had to reason over."""
    rr = ar.RunResult(
        mode="dryrun", date=DATE,
        evidence_coverage={"total_units": 5, "with_outcomes": 2,
                           "below_floor": 4, "at_or_above_floor": 1,
                           "min_evidence": 10},
        applied_counts={"edits": 0, "reversions": 0, "quarantines": 0})
    md = ar.render_digest(rr)
    assert "evidence" in md.lower()
    assert "below" in md.lower() or "MIN_EVIDENCE" in md


# --------------------------------------------------------------------------- #
# 5b. SP-8 B4 — the digest's USAGE-attribution coverage line (the dry-run arbiter)
# --------------------------------------------------------------------------- #

def _link_informed_by(conn, *, unit_id, signals):
    """Wire usage outcomes via informed_by edges (the SP-8 session-end attribution
    predicate) — session_event --informed_by--> memory."""
    for i, sig in enumerate(signals):
        ev = _event(conn, session_id=f"usage-{unit_id}-{i}", outcome_signal=sig,
                    title=f"usage-{unit_id}-{i}")
        memory_lib.record_link(
            conn, src_kind="session_event", src_id=str(ev),
            predicate="informed_by", dst_kind="memory", dst_id=unit_id, ts=TS)


def test_coverage_dict_has_usage_keys_no_informed_by(tmp_path):
    """SP-8 B4: outcome_evidence_coverage gains usage-specific keys. With only
    validated_as bookkeeping edges (no informed_by), the usage counts are 0 while
    the existing keys still reflect the bookkeeping evidence."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="book", index_hook="risk-manager")
    _link_outcomes(conn, unit_id="book", signals=["tests_passed"] * 11)  # validated_as
    cov = ar.outcome_evidence_coverage(conn)
    # Existing keys intact.
    for k in ("total_units", "with_outcomes", "below_floor", "at_or_above_floor",
              "min_evidence"):
        assert k in cov
    assert cov["with_outcomes"] == 1            # bookkeeping outcomes still counted
    assert cov["at_or_above_floor"] == 1        # 11 >= MIN_EVIDENCE
    # New usage-specific keys: zero, because there are no informed_by edges.
    assert cov["with_usage_outcomes"] == 0
    assert cov["usage_at_or_above_floor"] == 0


def test_coverage_dict_counts_informed_by_usage(tmp_path):
    """With ≥ MIN_EVIDENCE informed_by edges on a unit, the usage-specific counts
    reflect real usage attribution (distinct from bookkeeping)."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="used", index_hook="backtest")
    _link_informed_by(conn, unit_id="used", signals=["trade_win"] * ao.MIN_EVIDENCE)
    _save(conn, id="used-thin", index_hook="backtest")
    _link_informed_by(conn, unit_id="used-thin", signals=["trade_win"])  # 1 < floor
    cov = ar.outcome_evidence_coverage(conn)
    assert cov["with_usage_outcomes"] == 2          # both have >=1 informed_by
    assert cov["usage_at_or_above_floor"] == 1      # only the >=MIN_EVIDENCE one


def test_attribution_enabled_by_default(monkeypatch):
    monkeypatch.delenv("SP8_ATTRIBUTION_ENABLE", raising=False)
    _policy, _k, enabled = ar._attribution_policy_in_force()
    assert enabled is True                              # was False (opt-in)


def test_attribution_explicit_optout(monkeypatch):
    monkeypatch.setenv("SP8_ATTRIBUTION_ENABLE", "0")
    assert ar._attribution_policy_in_force()[2] is False


def test_render_digest_usage_attribution_lines_and_policy_gate_off(monkeypatch):
    """The rendered digest surfaces the usage-attribution lines + the policy-in-force
    line. With SP8_ATTRIBUTION_ENABLE explicitly opted out (=0) and no usage outcomes,
    the digest says attribution is disabled by design."""
    monkeypatch.setenv("SP8_ATTRIBUTION_ENABLE", "0")
    monkeypatch.delenv("SP8_ATTRIBUTION_POLICY", raising=False)
    monkeypatch.delenv("SP8_ATTRIBUTION_K", raising=False)
    rr = ar.RunResult(
        mode="dryrun", date=DATE,
        evidence_coverage={"total_units": 3, "with_outcomes": 1,
                           "below_floor": 3, "at_or_above_floor": 0,
                           "min_evidence": 10,
                           "with_usage_outcomes": 0, "usage_at_or_above_floor": 0},
        applied_counts={"edits": 0, "reversions": 0, "quarantines": 0})
    md = ar.render_digest(rr)
    # Usage-attribution lines present.
    assert "USAGE attribution" in md or "usage attribution" in md.lower()
    assert "informed_by" in md
    # Policy-in-force line present with defaults.
    assert "attribution policy in force" in md.lower()
    assert "top_k" in md
    assert "k=" in md
    assert "SP8_ATTRIBUTION_ENABLE" in md
    # Gate OFF: the narrative says disabled / arm it.
    assert ("disabled" in md.lower()) or ("arm it" in md.lower())
    # The OLD wording about the SP-6 backfill is gone (SP-8 supplies that attribution).
    assert "pending the SP-6 outcome-attribution backfill" not in md


def test_render_digest_usage_attribution_accruing_when_gate_on(monkeypatch):
    """With SP8_ATTRIBUTION_ENABLE ON but no usage outcomes yet, the digest says no
    session has attributed a usage outcome yet (coverage is accruing), and the
    policy-in-force line shows the env-set policy + k."""
    monkeypatch.setenv("SP8_ATTRIBUTION_ENABLE", "1")
    monkeypatch.setenv("SP8_ATTRIBUTION_POLICY", "all")
    monkeypatch.setenv("SP8_ATTRIBUTION_K", "3")
    rr = ar.RunResult(
        mode="dryrun", date=DATE,
        evidence_coverage={"total_units": 2, "with_outcomes": 0,
                           "below_floor": 2, "at_or_above_floor": 0,
                           "min_evidence": 10,
                           "with_usage_outcomes": 0, "usage_at_or_above_floor": 0},
        applied_counts={"edits": 0, "reversions": 0, "quarantines": 0})
    md = ar.render_digest(rr)
    assert "attribution policy in force" in md.lower()
    assert "all" in md
    assert "k=" in md and "3" in md
    # Gate ON shows on/enabled in the policy line.
    assert ("on" in md.lower()) or ("enabled" in md.lower())
    # Accruing narrative (gate on, but with_usage_outcomes==0).
    assert "accru" in md.lower()


def test_render_digest_handles_legacy_coverage_without_usage_keys():
    """Fail-open / back-compat: a coverage dict missing the new usage keys (an older
    serialized RunResult) renders without raising, defaulting usage counts to 0."""
    rr = ar.RunResult(
        mode="dryrun", date=DATE,
        evidence_coverage={"total_units": 1, "with_outcomes": 0,
                           "below_floor": 1, "at_or_above_floor": 0,
                           "min_evidence": 10},  # no usage keys
        applied_counts={"edits": 0, "reversions": 0, "quarantines": 0})
    md = ar.render_digest(rr)              # must not raise
    assert "USAGE attribution" in md or "usage attribution" in md.lower()


def test_coverage_failopen_keeps_minimal_dict(tmp_path):
    """outcome_evidence_coverage stays fail-open: a broken connection returns the
    minimal dict (now including the usage keys) and never raises."""
    conn = _open_temp_db(tmp_path)
    conn.close()                            # force read errors on every query
    cov = ar.outcome_evidence_coverage(conn)
    assert cov["total_units"] == 0
    assert cov["with_usage_outcomes"] == 0
    assert cov["usage_at_or_above_floor"] == 0


# =========================================================================== #
# 6. Fail-open — any error degrades to a no-op + one line, never wedges.
# =========================================================================== #

def test_failopen_on_track_error_is_noop(tmp_path, monkeypatch):
    """A blowing-up track (here: a runner that raises) degrades to a no-op result —
    the pass never raises out into the maintenance run."""
    conn = _open_temp_db(tmp_path)
    _save(conn, id="qa", body="On a VIX spike SELL premium", title="A")
    _save(conn, id="qb", body="On a VIX spike BUY premium", title="B",
          created_by="background_review")

    def boom(*a, **k):
        raise RuntimeError("runner exploded")

    repo, env = _init_git_repo(tmp_path)
    # Should NOT raise.
    res = ar.run_aggressive_pass(
        conn, repo_root=repo, date=DATE, ts=TS, probes=_probes(),
        embedder=_stub_embedder, runner=boom,
        briefings_dir=tmp_path / "briefings", git_env=env, export_fn=lambda: None)
    # the units survive untouched.
    for mid in ("qa", "qb"):
        row = conn.execute("SELECT status FROM memories WHERE id=?", (mid,)).fetchone()
        assert row["status"] == "active"
    assert res.applied_counts == {"edits": 0, "reversions": 0, "quarantines": 0}


def test_failopen_on_digest_write_error_does_not_raise(tmp_path, monkeypatch):
    """If the digest write itself fails (e.g. an unwritable dir), the pass still
    returns — the digest is best-effort, never a wedge."""
    monkeypatch.setenv(ab._DRYRUN_ENV, "1")
    conn = _open_temp_db(tmp_path)
    repo, env = _init_git_repo(tmp_path)
    # Point briefings_dir at a FILE so mkdir/write fails.
    bad = tmp_path / "afile"
    bad.write_text("x")
    res = ar.run_aggressive_pass(
        conn, repo_root=repo, date=DATE, ts=TS, probes=_probes(),
        embedder=_stub_embedder, runner=_quiet_runner(),
        briefings_dir=bad, git_env=env, export_fn=lambda: None)
    # No exception; digest_path is None (write failed, fail-open).
    assert res.digest_path is None


# =========================================================================== #
# 7. Audit jsonl row (the machine-audit half of §4e).
# =========================================================================== #

def test_audit_jsonl_row_written(tmp_path, monkeypatch):
    """Each run appends a machine-audit row to briefings/maintenance-logs/
    sp7-*.jsonl with the mode + the applied/proposed counts + the bound state."""
    monkeypatch.setenv(ab._DRYRUN_ENV, "1")
    conn = _open_temp_db(tmp_path)
    repo, env = _init_git_repo(tmp_path)
    logdir = tmp_path / "maintenance-logs"
    ar.run_aggressive_pass(
        conn, repo_root=repo, date=DATE, ts=TS, probes=_probes(),
        embedder=_stub_embedder, runner=_quiet_runner(),
        briefings_dir=tmp_path / "briefings", audit_dir=logdir, git_env=env,
        export_fn=lambda: None)
    rows = list((logdir).glob("sp7-*.jsonl"))
    assert rows, "no sp7 audit jsonl written"
    line = rows[0].read_text().strip().splitlines()[-1]
    obj = json.loads(line)
    assert obj["mode"] == "dryrun"
    assert "applied_counts" in obj
    assert "ts" in obj


# =========================================================================== #
# 8. OAuth-only + archive-never-delete guards (the standing gates).
# =========================================================================== #

def test_run_module_no_anthropic_sdk_import():
    """OAuth-only (HARD): this module makes its LLM calls ONLY through the tracks'
    injected runner (the OAuth `claude` CLI in a real run); it NEVER imports the
    anthropic SDK / API."""
    src = Path(ar.__file__).read_text()
    for forbidden in ("import anthropic", "from anthropic", "ANTHROPIC_API_KEY",
                      "messages.create", "cache_control", "api.anthropic.com"):
        assert forbidden not in src, f"forbidden OAuth-violating token: {forbidden}"


def test_run_module_archive_never_delete():
    """No destructive call anywhere — the orchestrator only composes reversible FSM
    transitions via the tracks; it never rm/os.remove/memory_lib.delete."""
    src = Path(ar.__file__).read_text()
    for forbidden in ("os.remove(", "shutil.rmtree(", ".unlink(", "memory_lib.delete(",
                      "DROP TABLE", "DELETE FROM memories"):
        assert forbidden not in src, f"destructive call present: {forbidden}"


def test_run_module_imports_clean():
    """The module imports with no claude CLI present, no fastembed, no network."""
    import importlib
    importlib.reload(ar)
    assert hasattr(ar, "run_aggressive_pass")
    assert hasattr(ar, "render_digest")
    assert hasattr(ar, "RunResult")


# =========================================================================== #
# 9. The Stage-2c SHELL wiring (run_maintenance.sh) — SHIPS DISABLED / cron-off.
# =========================================================================== #

_RUN_MAINT = Path(__file__).resolve().parent.parent.parent / "scripts" / "run_maintenance.sh"
def test_main_builds_and_threads_a_non_none_embedder(tmp_path, monkeypatch):
    """aggressive_run.main() must construct the engine's default embedder and
    thread it (NOT None) into run_aggressive_pass → the quarantine + eval tracks.
    We stub retrieval_core.default_embedder (so no fastembed loads) to a sentinel
    and capture what main() passes as `embedder`."""
    from ultra_memory import retrieval_core

    sentinel = object()
    monkeypatch.setattr(retrieval_core, "default_embedder", lambda: sentinel)

    db = tmp_path / "memory.db"
    memory_lib.open_memory_db(str(db)).close()
    repo, _env = _init_git_repo(tmp_path)

    captured = {}

    def _capture_pass(conn, *, embedder=None, **kwargs):
        captured["embedder"] = embedder
        return ar.RunResult(date=DATE)

    monkeypatch.setattr(ar, "run_aggressive_pass", _capture_pass)
    # DISABLE so the (stubbed-out) pass is a guaranteed no-op shape anyway.
    monkeypatch.setenv(ab._DISABLE_ENV, "1")

    rc = ar.main(["--db", str(db), "--repo-root", str(repo)])
    assert rc == 0
    assert "embedder" in captured, "main() did not call run_aggressive_pass"
    assert captured["embedder"] is not None, (
        "main() threaded embedder=None into run_aggressive_pass — memory recall "
        "(quarantine select_near_pairs + eval _probe_recall_ids) needs a real "
        "embedder; build retrieval_core.default_embedder() like consolidate_candidates")
    assert captured["embedder"] is sentinel, (
        "main() must thread the ENGINE default embedder, not some other object")


def test_main_embedder_failopen_degrades_when_fastembed_absent(tmp_path, monkeypatch):
    """Mirror consolidate_candidates' fail-open: if default_embedder() raises
    (fastembed absent in the maintenance env), main() must NOT crash — it degrades
    (logs) and still runs the pass (fail-open), never wedging the maintenance run."""
    from ultra_memory import retrieval_core

    def _boom():
        raise RuntimeError("fastembed not installed")

    monkeypatch.setattr(retrieval_core, "default_embedder", _boom)

    db = tmp_path / "memory.db"
    memory_lib.open_memory_db(str(db)).close()
    repo, _env = _init_git_repo(tmp_path)

    called = {"n": 0}

    def _capture_pass(conn, *, embedder=None, **kwargs):
        called["n"] += 1
        return ar.RunResult(date=DATE)

    monkeypatch.setattr(ar, "run_aggressive_pass", _capture_pass)
    monkeypatch.setenv(ab._DISABLE_ENV, "1")

    rc = ar.main(["--db", str(db), "--repo-root", str(repo)])  # must NOT raise
    assert rc == 0
    assert called["n"] == 1, "the pass must still run (fail-open) when no embedder"
