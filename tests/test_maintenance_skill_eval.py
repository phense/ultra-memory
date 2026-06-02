"""Tests for skill_eval.py — SP-10 Stage 4 trigger-probe eval-gate."""
import sys
from pathlib import Path

import pytest


from ultra_memory.maintenance import skill_eval as se  # noqa: E402
from ultra_memory.maintenance import skill_fs as sf  # noqa: E402
from ultra_memory.claude_cli import OAuthViolation  # noqa: E402

FAKE_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "tok-test"}
# Plugin test sits one level under the repo root (tests/), so two parents reach it
# (the Trading original was tests/maintenance/ → three). The probe corpus fixture
# is copied into the plugin at tests/fixtures/skill_trigger_probes.json.
REPO = Path(__file__).resolve().parent.parent
CORPUS = se.load_corpus(REPO / "tests" / "fixtures" / "skill_trigger_probes.json")
STATICS = {
    "risk-manager": "MUST use BEFORE any trading action — placing an order, sizing a position, setting a stop loss.",
    "backtest": "Use when designing or backtesting a trading strategy against history.",
}


def _cand(slug="gen-foo", description="Use when tuning the vol-vibes calendar entry timing."):
    return sf.GeneratedSkill(slug=slug, description=description, body="# b",
                             index_hook=slug, source_lesson_ids=["L1"])


def test_token_cosine():
    assert se.token_cosine("a b c", "a b c") == pytest.approx(1.0)
    assert se.token_cosine("a b c", "x y z") == 0.0


def test_tier_a_reject_on_overlap():
    hijack = _cand(description=STATICS["risk-manager"])  # identical to a static desc
    assert se.tier_a_reject(hijack.description, STATICS) is not None
    narrow = _cand()  # distinct vocabulary
    assert se.tier_a_reject(narrow.description, STATICS) is None


def test_coverage_gaps():
    assert se.coverage_gaps(["risk-manager"], CORPUS) == []
    assert se.coverage_gaps(["risk-manager", "no-such-skill"], CORPUS) == ["no-such-skill"]


def test_gate_admits_clean_candidate():
    rep = se.run_trigger_gate(_cand(), static_descriptions=STATICS, corpus=CORPUS,
                              probe_fn=lambda q, c: False)
    assert rep.admit is True and rep.verdict == "admit" and rep.candidate_fp == 0


def test_gate_rejects_tier_a():
    rep = se.run_trigger_gate(_cand(description=STATICS["risk-manager"]),
                              static_descriptions=STATICS, corpus=CORPUS,
                              probe_fn=lambda q, c: False)
    assert rep.admit is False and rep.verdict == "reject" and rep.tier_a_hit


def test_gate_rejects_tier_b_hijack():
    rep = se.run_trigger_gate(_cand(), static_descriptions=STATICS, corpus=CORPUS,
                              probe_fn=lambda q, c: True)  # candidate steals every probe
    assert rep.admit is False and rep.verdict == "reject" and rep.candidate_fp > 0


def test_gate_holds_on_coverage_gap():
    statics = dict(STATICS, **{"uncovered-skill": "x"})
    rep = se.run_trigger_gate(_cand(), static_descriptions=statics, corpus=CORPUS,
                              probe_fn=lambda q, c: False)
    assert rep.admit is False and rep.verdict == "hold" and "coverage gap" in rep.reason


def test_gate_holds_on_empty_hijack_probes():
    corpus = [{"query": "q", "expect": "risk-manager", "should_trigger": False}]
    rep = se.run_trigger_gate(_cand(), static_descriptions={"risk-manager": "x"},
                              corpus=corpus, probe_fn=lambda q, c: False)
    assert rep.admit is False and rep.verdict == "hold"


def test_gate_probe_error_fails_closed():
    def boom(q, c):
        raise RuntimeError("probe spawn failed")
    rep = se.run_trigger_gate(_cand(), static_descriptions=STATICS, corpus=CORPUS,
                              probe_fn=boom)
    assert rep.admit is False and rep.verdict == "reject" and rep.candidate_fp > 0


def test_gate_holds_on_budget():
    rep = se.run_trigger_gate(_cand(), static_descriptions=STATICS, corpus=CORPUS,
                              probe_fn=lambda q, c: False, budget_fn=lambda c: False)
    assert rep.admit is False and rep.verdict == "hold" and "budget" in rep.reason


def test_probe_fires_oauth_and_parse(tmp_path):
    import types
    fired_stream = '{"type":"stream_event","event":{"content_block":{"type":"tool_use","name":"Skill","input":{"skill":"gen-foo-probe"}}}}'
    quiet_stream = '{"type":"text","text":"no skill used"}'

    def runner_fired(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout=fired_stream, stderr="")

    def runner_quiet(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout=quiet_stream, stderr="")

    assert se.probe_fires("q", "gen-foo", "desc", repo_root=tmp_path,
                          runner=runner_fired, env=FAKE_ENV) is True
    assert se.probe_fires("q", "gen-foo", "desc", repo_root=tmp_path,
                          runner=runner_quiet, env=FAKE_ENV) is False
    # the ephemeral probe command-file is cleaned up
    assert not (tmp_path / ".claude" / "commands" / "gen-foo-probe.md").exists()
    # OAuth-only: no token -> refuse to spawn
    with pytest.raises(OAuthViolation):
        se.probe_fires("q", "gen-foo", "desc", repo_root=tmp_path,
                       runner=runner_quiet, env={})


def test_stream_mentions_ignores_system_init():
    # the real `claude -p --output-format stream-json` emits a system/init event
    # listing slash_commands (incl. the probe) + tools (incl. "Skill"); a substring
    # match fired on it → vacuous gate (the 2026-06-01 review blocker). It must NOT fire.
    init = ('{"type":"system","subtype":"init","slash_commands":["gen-foo-probe"],'
            '"tools":["Skill","Read","Bash"]}')
    quiet = init + "\n" + '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}'
    assert se._stream_mentions(quiet, "gen-foo-probe") is False
    fired = init + "\n" + ('{"type":"assistant","message":{"content":'
                           '[{"type":"tool_use","name":"Skill","input":{"skill":"gen-foo-probe"}}]}}')
    assert se._stream_mentions(fired, "gen-foo-probe") is True
    # stream_event wrapper (content_block under event)
    fired2 = ('{"type":"stream_event","event":{"type":"content_block_start",'
              '"content_block":{"type":"tool_use","name":"Read","input":{"file_path":"x/gen-foo-probe.md"}}}}')
    assert se._stream_mentions(fired2, "gen-foo-probe") is True


def test_coverage_requires_should_trigger_probe():
    neg_only = [{"query": "q", "expect": "backtest", "should_trigger": False}]
    assert se.coverage_gaps(["backtest"], neg_only) == ["backtest"]  # negative-only != covered
    both = neg_only + [{"query": "q2", "expect": "backtest", "should_trigger": True}]
    assert se.coverage_gaps(["backtest"], both) == []


def test_read_all_invocable_includes_plugins_and_excludes_generated():
    proj = se.read_static_skill_descriptions(REPO / ".claude" / "skills")
    alld = se.read_all_invocable_skill_descriptions(REPO)
    assert set(proj).issubset(set(alld))               # project skills always present
    assert not any(k.startswith("gen-") for k in alld)  # generated skills excluded
    assert len(alld) >= len(proj)                       # plugins are a (best-effort) superset


def test_estimate_listing_budget():
    descs = {"a": "x" * 100, "b": "y" * 100}
    assert se.estimate_listing_budget_ok("z" * 100, descs, budget_chars=1000) is True
    assert se.estimate_listing_budget_ok("z" * 100, descs, budget_chars=250) is False

# NOTE: the Trading-original test_corpus_covers_all_live_project_skills (the lint that
# the checked-in corpus covers every live `<project>/.claude/skills/*`) is a CONSUMER
# coverage test — it asserts a non-empty project skills dir, which the plugin (no
# consumer .claude/skills) does not have. It stays in the consumer (Trading), not here.
