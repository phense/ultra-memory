"""Tests for aggressive_bounds.py — SP-7 §4c (bounded blast radius + halt-on-
exceed) + §4d (pre-run git checkpoint + clean-tree precondition) + §4f (kill
switch: SP7_AGGRESSIVE_DISABLE / SP7_AGGRESSIVE_DRYRUN + fail-open).

THE SAFETY WALL LIVES IN THE APPLY PATH (code), NEVER ONLY THE PROMPT. The LLM
*proposes* a plan; the bounds layer *enforces* the caps. The bound is the
structural defense against the 2026-05-24 95%-blast: even a catastrophically
wrong plan can only touch a handful of units before the cap trips — and a plan
OVER the cap applies NONE of the over-cap class (halt-on-exceed, the spec §4c
'stop and ask', NOT truncate-and-continue).

HARD INVARIANTS under test:
  * a plan over a per-class cap → that class is HALTED (applies none of it),
    logs 'bound exceeded: proposed=K cap=N', and the halt surfaces in the digest
    (NOT silently truncate-the-first-N);
  * a per-class plan AT-or-UNDER the cap passes through unchanged;
  * a global per-period aggregate cap (a `meta` counter) blocks stacked re-runs
    even when each individual run is under its per-run caps;
  * the pre-run git checkpoint refuses to start (fail-soft skip) on a DIRTY tree
    — no checkpoint → no aggressive write; a CLEAN tree gets a tag + export
    snapshot (tested against a TEMP git repo, never the live repo);
  * SP7_AGGRESSIVE_DISABLE short-circuits the whole pass to a no-op + one line;
  * SP7_AGGRESSIVE_DRYRUN plans + digests but applies NOTHING;
  * fail-open: any error in the bounds/checkpoint/gate path degrades to a no-op
    + one diagnostic line — it never wedges maintenance, never raises out.

These tests NEVER touch the live memory.db, NEVER touch the live git repo, NEVER
spawn `claude`, NEVER load a real embedder. They run against a temp DB + a temp
git repo + synthetic plans.
"""
from pathlib import Path

import pytest

from ultra_memory import memory_lib
from ultra_memory.maintenance import aggressive_bounds as ab


TS = "2026-05-31T00:00:00Z"


# --------------------------------------------------------------------------- #
# Plan helpers — a "plan" is the LLM's proposed actions, partitioned by class.
# --------------------------------------------------------------------------- #

def _plan(*, edits=0, reversions=0, quarantines=0):
    """Build a synthetic plan: each class is a list of opaque action dicts."""
    return {
        "edits": [{"old_id": f"e{i}"} for i in range(edits)],
        "reversions": [{"regressed_id": f"r{i}"} for i in range(reversions)],
        "quarantines": [{"id_a": f"qa{i}", "id_b": f"qb{i}"} for i in range(quarantines)],
    }


# --------------------------------------------------------------------------- #
# 4c. Per-class caps + halt-on-exceed
# --------------------------------------------------------------------------- #

def test_default_caps_are_conservative():
    """Spec §4c: MAX_EDITS=3, MAX_REVERSIONS=3, MAX_QUARANTINES=5."""
    assert ab.MAX_EDITS_PER_RUN == 3
    assert ab.MAX_REVERSIONS_PER_RUN == 3
    assert ab.MAX_QUARANTINES_PER_RUN == 5


def test_under_cap_plan_passes_through_unchanged():
    """A plan at-or-under every cap is admitted whole; no bound is hit."""
    plan = _plan(edits=3, reversions=2, quarantines=5)
    res = ab.enforce_caps(plan)
    assert len(res.admitted["edits"]) == 3
    assert len(res.admitted["reversions"]) == 2
    assert len(res.admitted["quarantines"]) == 5
    assert res.bounds_hit == []


def test_over_cap_edits_halts_the_class_not_truncate():
    """HALT-ON-EXCEED (spec §4c): a plan with MORE edits than the cap applies
    NONE of the edits class (not the first N), logs the bound, and surfaces it."""
    plan = _plan(edits=7, reversions=1, quarantines=1)
    res = ab.enforce_caps(plan)
    # edits class HALTED — zero admitted, not 3.
    assert res.admitted["edits"] == []
    # the under-cap classes still pass.
    assert len(res.admitted["reversions"]) == 1
    assert len(res.admitted["quarantines"]) == 1
    # the bound is recorded with the exact proposed=K cap=N shape.
    assert any(
        b["cls"] == "edits" and b["proposed"] == 7 and b["cap"] == 3
        for b in res.bounds_hit)


def test_over_cap_message_has_proposed_cap_shape():
    """The log line is exactly 'bound exceeded: proposed=K cap=N' (spec §4c)."""
    plan = _plan(edits=9)
    res = ab.enforce_caps(plan)
    msg = ab.format_bound_message(res.bounds_hit[0])
    assert "bound exceeded" in msg
    assert "proposed=9" in msg
    assert "cap=3" in msg


def test_each_class_capped_independently():
    """Over-cap in one class HALTS only that class; the others are unaffected."""
    plan = _plan(edits=1, reversions=8, quarantines=2)
    res = ab.enforce_caps(plan)
    assert len(res.admitted["edits"]) == 1            # under cap → kept
    assert res.admitted["reversions"] == []           # over cap → HALTED
    assert len(res.admitted["quarantines"]) == 2      # under cap → kept
    assert [b["cls"] for b in res.bounds_hit] == ["reversions"]


def test_exactly_at_cap_is_admitted():
    """AT the cap (==) is admitted; only STRICTLY OVER halts."""
    plan = _plan(edits=ab.MAX_EDITS_PER_RUN, quarantines=ab.MAX_QUARANTINES_PER_RUN)
    res = ab.enforce_caps(plan)
    assert len(res.admitted["edits"]) == ab.MAX_EDITS_PER_RUN
    assert len(res.admitted["quarantines"]) == ab.MAX_QUARANTINES_PER_RUN
    assert res.bounds_hit == []


# --------------------------------------------------------------------------- #
# 4c. Global per-period aggregate cap (the meta counter — blocks stacked re-runs)
# --------------------------------------------------------------------------- #

def _open_temp_db(tmp_path):
    return memory_lib.open_memory_db(str(tmp_path / "memory.db"))


def test_period_cap_blocks_stacked_reruns(tmp_path):
    """A global per-period aggregate cap across runs (a `meta` counter): two
    under-cap runs in the same period whose SUM exceeds the period cap → the
    second run's over-the-period classes are halted (spec §4c: 're-runs cannot
    stack')."""
    conn = _open_temp_db(tmp_path)
    period = "2026-05"
    # Run 1: 3 edits (== per-run cap, but consumes the whole period budget if it's 3).
    res1 = ab.enforce_caps(_plan(edits=3), conn=conn, period=period,
                           period_cap_edits=3)
    assert len(res1.admitted["edits"]) == 3
    ab.commit_period_usage(conn, period=period, applied=res1.admitted, ts=TS)
    # Run 2 same period: even 1 more edit exceeds the period budget (3 already used).
    res2 = ab.enforce_caps(_plan(edits=1), conn=conn, period=period,
                           period_cap_edits=3)
    assert res2.admitted["edits"] == []
    assert any(b.get("scope") == "period" and b["cls"] == "edits"
               for b in res2.bounds_hit)


def test_period_cap_resets_next_period(tmp_path):
    """A NEW period starts with a fresh budget — last period's usage does not
    carry over."""
    conn = _open_temp_db(tmp_path)
    ab.commit_period_usage(conn, period="2026-05",
                           applied={"edits": _plan(edits=3)["edits"]}, ts=TS)
    # A different period — fresh budget.
    res = ab.enforce_caps(_plan(edits=3), conn=conn, period="2026-06",
                          period_cap_edits=3)
    assert len(res.admitted["edits"]) == 3
    assert res.bounds_hit == []


def test_period_cap_absent_conn_is_noop(tmp_path):
    """With no conn (period tracking off), only the per-run caps apply — the
    period gate is skipped cleanly (it is an ADDITIVE second cap)."""
    res = ab.enforce_caps(_plan(edits=3))   # no conn
    assert len(res.admitted["edits"]) == 3
    assert res.bounds_hit == []


# --------------------------------------------------------------------------- #
# 4d. Pre-run git checkpoint + clean-tree precondition (against a TEMP repo)
# --------------------------------------------------------------------------- #

def _init_temp_git_repo(tmp_path):
    """A real, isolated git repo in tmp — never the live repo."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    import os
    e = {**os.environ, **env}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=e)
    (repo / "wiki").mkdir()
    (repo / "wiki" / "seed.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=e)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True, env=e)
    return repo, e


def test_checkpoint_clean_tree_creates_tag(tmp_path):
    """A CLEAN tree → the checkpoint creates the pre-sp7-aggressive-<date> tag
    and reports ok=True. (Against a TEMP repo, never the live repo.)"""
    import subprocess
    repo, e = _init_temp_git_repo(tmp_path)
    res = ab.pre_run_checkpoint(repo_root=repo, date="2026-05-31",
                                export_fn=lambda: None, env=e)
    assert res.ok is True
    assert res.tag == "pre-sp7-aggressive-2026-05-31"
    tags = subprocess.run(["git", "tag"], cwd=repo, capture_output=True,
                          text=True, env=e).stdout
    assert "pre-sp7-aggressive-2026-05-31" in tags


def test_checkpoint_dirty_tree_skips_failsoft(tmp_path):
    """The exact 2026-05-24 gap: a DIRTY tree makes the checkpoint ambiguous, so
    the pass REFUSES TO START (fail-soft skip) — ok=False, NO tag, and the
    caller must not apply any aggressive write."""
    import subprocess
    repo, e = _init_temp_git_repo(tmp_path)
    (repo / "wiki" / "dirty.md").write_text("uncommitted\n")  # untracked = dirty
    res = ab.pre_run_checkpoint(repo_root=repo, date="2026-05-31",
                                export_fn=lambda: None, env=e)
    assert res.ok is False
    assert "dirty" in (res.reason or "").lower()
    tags = subprocess.run(["git", "tag"], cwd=repo, capture_output=True,
                          text=True, env=e).stdout
    assert "pre-sp7-aggressive-2026-05-31" not in tags  # no checkpoint made


def test_checkpoint_runs_export_snapshot_on_clean(tmp_path):
    """On a clean tree the checkpoint ALSO triggers the memory_export snapshot
    (spec §4d: tag BOTH roots + a memory_export snapshot)."""
    repo, e = _init_temp_git_repo(tmp_path)
    called = {"n": 0}

    def _export():
        called["n"] += 1

    res = ab.pre_run_checkpoint(repo_root=repo, date="2026-05-31",
                                export_fn=_export, env=e)
    assert res.ok is True
    assert called["n"] == 1


def test_checkpoint_export_failure_is_failsoft(tmp_path):
    """If the export snapshot raises, the checkpoint fails SOFT (ok=False) — it
    never lets the pass proceed on a half-made checkpoint, and never raises out."""
    repo, e = _init_temp_git_repo(tmp_path)

    def _boom():
        raise RuntimeError("export blew up")

    res = ab.pre_run_checkpoint(repo_root=repo, date="2026-05-31",
                                export_fn=_boom, env=e)
    assert res.ok is False


def test_rollback_command_is_documented(tmp_path):
    """Spec §4d: the checkpoint result carries the exact one-command rollback so
    reversibility is DOCUMENTED, not merely theoretical."""
    repo, e = _init_temp_git_repo(tmp_path)
    res = ab.pre_run_checkpoint(repo_root=repo, date="2026-05-31",
                                export_fn=lambda: None, env=e)
    assert "pre-sp7-aggressive-2026-05-31" in res.rollback_command
    assert "git reset" in res.rollback_command or "git revert" in res.rollback_command


# --------------------------------------------------------------------------- #
# 4f. Kill switch — SP7_AGGRESSIVE_DISABLE / SP7_AGGRESSIVE_DRYRUN
# --------------------------------------------------------------------------- #

def test_disable_default_present_is_noop(monkeypatch):
    """Ships DISABLED: with SP7_AGGRESSIVE_DISABLE present (any value) the whole
    pass is a no-op. The aggressive pass is opt-in to even run."""
    monkeypatch.setenv("SP7_AGGRESSIVE_DISABLE", "1")
    assert ab.is_disabled() is True


def test_disable_absent_means_enabled(monkeypatch):
    monkeypatch.delenv("SP7_AGGRESSIVE_DISABLE", raising=False)
    assert ab.is_disabled() is False


def test_disable_empty_string_still_disables(monkeypatch):
    """'present' = set, even to empty — the kill switch is presence-based per the
    spec wording ('default present'). An operator removes the var to enable."""
    monkeypatch.setenv("SP7_AGGRESSIVE_DISABLE", "")
    assert ab.is_disabled() is True


def test_dryrun_flag(monkeypatch):
    monkeypatch.setenv("SP7_AGGRESSIVE_DRYRUN", "1")
    assert ab.is_dry_run() is True
    monkeypatch.delenv("SP7_AGGRESSIVE_DRYRUN", raising=False)
    assert ab.is_dry_run() is False


def test_run_gate_disabled_short_circuits(monkeypatch):
    """run_gate() is the single entry the orchestrator calls. DISABLED → returns
    a 'noop' decision with one log line, applies nothing, makes no checkpoint."""
    monkeypatch.setenv("SP7_AGGRESSIVE_DISABLE", "1")
    monkeypatch.delenv("SP7_AGGRESSIVE_DRYRUN", raising=False)
    log = []
    decision = ab.run_gate(log=log.append)
    assert decision.mode == "noop"
    assert decision.may_apply is False
    assert len(log) == 1                       # exactly one line
    assert "disable" in log[0].lower()


def test_run_gate_dryrun_plans_but_no_apply(monkeypatch):
    """DRYRUN → may_apply is False (plan + digest, apply NOTHING), mode='dryrun'."""
    monkeypatch.delenv("SP7_AGGRESSIVE_DISABLE", raising=False)
    monkeypatch.setenv("SP7_AGGRESSIVE_DRYRUN", "1")
    decision = ab.run_gate(log=lambda m: None)
    assert decision.mode == "dryrun"
    assert decision.may_apply is False


def test_run_gate_enabled_live_may_apply(monkeypatch):
    """Both flags off → mode='live', may_apply True (the only mode that writes)."""
    monkeypatch.delenv("SP7_AGGRESSIVE_DISABLE", raising=False)
    monkeypatch.delenv("SP7_AGGRESSIVE_DRYRUN", raising=False)
    decision = ab.run_gate(log=lambda m: None)
    assert decision.mode == "live"
    assert decision.may_apply is True


def test_run_gate_disable_beats_dryrun(monkeypatch):
    """DISABLE takes precedence over DRYRUN — a disabled pass does not even plan."""
    monkeypatch.setenv("SP7_AGGRESSIVE_DISABLE", "1")
    monkeypatch.setenv("SP7_AGGRESSIVE_DRYRUN", "1")
    decision = ab.run_gate(log=lambda m: None)
    assert decision.mode == "noop"
    assert decision.may_apply is False


# --------------------------------------------------------------------------- #
# Fail-open — any error degrades to a no-op + one line, never wedges/raises
# --------------------------------------------------------------------------- #

def test_enforce_caps_failopen_on_bad_plan():
    """A malformed plan (not the expected dict shape) → fail-open: caps to an
    empty admitted set + a diagnostic bound, never raises."""
    res = ab.enforce_caps({"edits": "not-a-list"})   # wrong type
    assert res.admitted["edits"] == []
    assert any(b.get("scope") == "error" for b in res.bounds_hit)


def test_run_gate_failopen_on_env_read_error(monkeypatch):
    """If reading the env raises (forced), run_gate degrades to a SAFE noop, not
    a crash (fail-open + fail-CLOSED-to-safety: an error means do-nothing)."""
    def _boom(*a, **k):
        raise RuntimeError("env explode")
    monkeypatch.setattr(ab.os.environ, "get", _boom)
    log = []
    decision = ab.run_gate(log=log.append)
    assert decision.mode == "noop"
    assert decision.may_apply is False
    assert len(log) == 1


def test_checkpoint_failopen_on_git_error(tmp_path):
    """A non-repo path (git fails) → the checkpoint fails SOFT (ok=False), never
    raises out into the maintenance run."""
    res = ab.pre_run_checkpoint(repo_root=tmp_path / "not-a-repo",
                                date="2026-05-31", export_fn=lambda: None)
    assert res.ok is False


# --------------------------------------------------------------------------- #
# OAuth-only + archive-never-delete guards — the bounds module is pure policy
# --------------------------------------------------------------------------- #

def test_bounds_module_no_anthropic_sdk_import():
    src = Path(ab.__file__).read_text()
    for forbidden in ("import anthropic", "from anthropic", "ANTHROPIC_API_KEY",
                      "messages.create", "cache_control", "api.anthropic.com"):
        assert forbidden not in src, f"OAuth-only violation: {forbidden!r} in bounds"


def test_bounds_module_never_deletes():
    """The bounds/checkpoint layer is non-destructive: it gates + tags + exports;
    it never rm/deletes content (archive-never-delete is structural — the only
    git verbs it runs are status/tag/add, never a destructive reset of content)."""
    src = Path(ab.__file__).read_text()
    for forbidden in ("os.remove(", "shutil.rmtree(", ".unlink(",
                      "memory_lib.delete(", "rm -rf", "git reset --hard"):
        assert forbidden not in src, f"destructive call {forbidden!r} in bounds"
