"""Tests for skill_synthesize.py — SP-10 Stage 3 induction pass (planning only)."""
import json
import sys
import types
from pathlib import Path

import pytest


from ultra_memory.maintenance import skill_synthesize as ss  # noqa: E402
from ultra_memory.maintenance.aggressive_wall import ForbiddenTargetError  # noqa: E402
from ultra_memory import memory_lib  # noqa: E402

TS = "2026-06-01T00:00:00Z"
FAKE_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "tok-test"}


def _conn(tmp_path):
    return memory_lib.open_memory_db(str(tmp_path / "memory.db"))


def _lesson(conn, lid, domain, *, weight=1.0, created_by="background_review",
            pinned=False, body="a durable lesson about the domain"):
    memory_lib.save_memory(conn, id=lid, type="learning", title="L", body=body,
                           ts=TS, index_hook=domain, node_type="learning",
                           created_by=created_by)
    if weight != 1.0:
        memory_lib.set_outcome_weight(conn, id=lid, weight=weight, ts=TS)
    if pinned:
        memory_lib.set_pinned(conn, id=lid, pinned=True, ts=TS, reason="t")


def _runner(payload):
    def runner(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload),
                                     stderr="")
    return runner


def _good_payload(slug, lesson_ids):
    return {"skill": {"name": slug, "description": "Use when tuning the domain.",
                      "body": "# proc\n\ndo it", "paths": ["scripts/**"],
                      "source_lesson_ids": list(lesson_ids)}}


def test_derive_slug():
    assert ss.derive_slug("backtest") == "gen-backtest"
    assert ss.derive_slug("risk-manager") == "gen-risk-manager"
    # INJECTIVITY (2026-06-01 review): distinct domains must yield distinct slugs.
    # A 'gen-foo' domain (recursive) → 'gen-gen-foo', distinct from a 'foo' domain.
    assert ss.derive_slug("gen-foo") == "gen-gen-foo"
    assert ss.derive_slug("foo") == "gen-foo"
    assert ss.derive_slug("backtest") != ss.derive_slug("gen-backtest")
    assert ss.derive_slug("Vol Vibes!") == "gen-vol-vibes"


def test_select_cluster_meets_threshold(tmp_path):
    conn = _conn(tmp_path)
    for i in range(3):
        _lesson(conn, f"b{i}", "backtest")
    clusters = ss.select_induction_clusters(conn, n=3, theta_w=1.0)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["domain"] == "backtest" and c["slug"] == "gen-backtest" and c["n"] == 3
    assert set(c["lesson_ids"]) == {"b0", "b1", "b2"}


def test_select_n_threshold(tmp_path):
    conn = _conn(tmp_path)
    _lesson(conn, "b0", "backtest")
    _lesson(conn, "b1", "backtest")
    assert ss.select_induction_clusters(conn, n=3, theta_w=1.0) == []


def test_select_theta_w(tmp_path):
    conn = _conn(tmp_path)
    for i in range(3):
        _lesson(conn, f"b{i}", "backtest", weight=0.9)  # demoted
    assert ss.select_induction_clusters(conn, n=3, theta_w=1.0) == []
    assert len(ss.select_induction_clusters(conn, n=3, theta_w=0.5)) == 1


def test_select_includes_backfill_import_provenance(tmp_path):
    # SP-10 visibility is by node_type='learning', NOT created_by. The cold-start
    # backfill (created_by='backfill_import') MUST seed synthesis — that is the whole
    # point of seeding the store so a fresh install isn't at zero. Provenance gates
    # MUTABILITY (SP-7 MUTABLE_PROVENANCES), not VISIBILITY (SP-10 selection).
    conn = _conn(tmp_path)
    for i in range(3):
        _lesson(conn, f"b{i}", "backtest", created_by="backfill_import")
    clusters = ss.select_induction_clusters(conn, n=3, theta_w=1.0)
    assert len(clusters) == 1
    assert clusters[0]["domain"] == "backtest" and clusters[0]["n"] == 3


def test_select_counts_mixed_provenance(tmp_path):
    # human + import + backfill_import learnings in one domain all count toward the
    # cluster — selection is provenance-agnostic.
    conn = _conn(tmp_path)
    _lesson(conn, "m0", "backtest", created_by="human")
    _lesson(conn, "m1", "backtest", created_by="import")
    _lesson(conn, "m2", "backtest", created_by="backfill_import")
    clusters = ss.select_induction_clusters(conn, n=3, theta_w=1.0)
    assert len(clusters) == 1 and clusters[0]["n"] == 3
    assert set(clusters[0]["lesson_ids"]) == {"m0", "m1", "m2"}


def test_select_excludes_generated_skill_backing_rows(tmp_path):
    # Regression guard: a generated skill's own backing row (node_type='generated_skill')
    # must NOT be counted as a lesson, else a skill would re-induce itself. The
    # node_type='learning' predicate must survive the created_by decoupling.
    conn = _conn(tmp_path)
    for i in range(3):
        _lesson(conn, f"b{i}", "backtest", created_by="backfill_import")
    memory_lib.save_memory(conn, id="genrow", type="learning", title="G",
                           body="backing", ts=TS, index_hook="backtest",
                           node_type="generated_skill", created_by="background_review")
    clusters = ss.select_induction_clusters(conn, n=3, theta_w=1.0)
    assert len(clusters) == 1
    assert "genrow" not in set(clusters[0]["lesson_ids"]) and clusters[0]["n"] == 3


def test_draft_happy(tmp_path):
    conn = _conn(tmp_path)
    for i in range(3):
        _lesson(conn, f"b{i}", "backtest")
    out = ss.draft(conn, repo_root=tmp_path / "repo",
                   runner=_runner(_good_payload("gen-backtest", ["b0", "b1", "b2"])),
                   ts=TS, env=FAKE_ENV)
    assert out["skill"] is not None
    assert out["skill"].slug == "gen-backtest"
    assert out["skill"].index_hook == "gen-backtest"
    assert out["skill"].source_lesson_ids == ["b0", "b1", "b2"]


def test_draft_synthesizes_from_backfill_import_source(tmp_path):
    # Synthesis READS source lessons to induce a NEW skill — it never mutates them — so
    # source eligibility is provenance-agnostic. The cold-start backfill seed
    # (created_by='backfill_import') is immutable to SP-7 but MUST be readable by SP-10,
    # else the seed can never become a skill. (Was blocked by the assert_mutable funnel —
    # the same mutability-vs-visibility conflation as the cluster-selection fix.)
    conn = _conn(tmp_path)
    for i in range(3):
        _lesson(conn, f"b{i}", "backtest", created_by="backfill_import")
    out = ss.draft(conn, repo_root=tmp_path / "repo",
                   runner=_runner(_good_payload("gen-backtest", ["b0", "b1", "b2"])),
                   ts=TS, env=FAKE_ENV)
    assert out["skill"] is not None and out["skill"].slug == "gen-backtest"


def test_draft_synthesizes_from_import_and_human_sources(tmp_path):
    # import + human learnings are also readable sources (additive, eval-gated, reversible;
    # the source row is untouched). Only PINNED stays protected (next test).
    conn = _conn(tmp_path)
    _lesson(conn, "b0", "backtest", created_by="import")
    _lesson(conn, "b1", "backtest", created_by="human")
    _lesson(conn, "b2", "backtest", created_by="backfill_import")
    out = ss.draft(conn, repo_root=tmp_path / "repo",
                   runner=_runner(_good_payload("gen-backtest", ["b0", "b1", "b2"])),
                   ts=TS, env=FAKE_ENV)
    assert out["skill"] is not None


def test_draft_off_slug_dropped(tmp_path):
    conn = _conn(tmp_path)
    for i in range(3):
        _lesson(conn, f"b{i}", "backtest")
    out = ss.draft(conn, repo_root=tmp_path / "repo",
                   runner=_runner(_good_payload("gen-other", ["b0", "b1", "b2"])),
                   ts=TS, env=FAKE_ENV)
    assert out["skill"] is None


def test_draft_ungrounded_dropped(tmp_path):
    conn = _conn(tmp_path)
    for i in range(3):
        _lesson(conn, f"b{i}", "backtest")
    out = ss.draft(conn, repo_root=tmp_path / "repo",
                   runner=_runner(_good_payload("gen-backtest", ["b0", "L999"])),
                   ts=TS, env=FAKE_ENV)
    assert out["skill"] is None


def test_draft_null_skill(tmp_path):
    conn = _conn(tmp_path)
    for i in range(3):
        _lesson(conn, f"b{i}", "backtest")
    out = ss.draft(conn, repo_root=tmp_path / "repo",
                   runner=_runner({"skill": None}), ts=TS, env=FAKE_ENV)
    assert out["skill"] is None


def test_draft_halts_on_pinned_source(tmp_path):
    conn = _conn(tmp_path)
    _lesson(conn, "b0", "backtest")
    _lesson(conn, "b1", "backtest")
    _lesson(conn, "b2", "backtest", pinned=True)  # agent-authored but pinned
    with pytest.raises(ForbiddenTargetError):
        ss.draft(conn, repo_root=tmp_path / "repo",
                 runner=_runner(_good_payload("gen-backtest", ["b0", "b1", "b2"])),
                 ts=TS, env=FAKE_ENV)


def test_draft_skips_existing_skill_domain(tmp_path):
    # A gen-<existing-skill> would hijack its static namesake → the eval-gate always
    # rejects it. Skip such a domain BEFORE drafting (no wasted run_claude/eval cost);
    # those domains are augmented via their per-skill Learnings.md instead.
    conn = _conn(tmp_path)
    for i in range(3):
        _lesson(conn, f"a{i}", "backtest")  # 'backtest' IS a static skill
    called = {"n": 0}
    def runner(cmd, **kw):
        called["n"] += 1
        return types.SimpleNamespace(
            returncode=0, stdout=json.dumps(_good_payload("gen-backtest", ["a0", "a1", "a2"])),
            stderr="")
    out = ss.draft(conn, repo_root=tmp_path / "repo", static_skill_names={"backtest"},
                   runner=runner, ts=TS, env=FAKE_ENV)
    assert out["skill"] is None and called["n"] == 0   # skipped before any draft call
    assert "no eligible cluster" in out["reason"]


def test_draft_skips_prefixed_plugin_skill_by_suffix(tmp_path):
    # A plugin skill's index_hook is prefixed ('superpowers:X') but its discovered name
    # is the bare 'X' → the colon-suffix must match so it's skipped too.
    conn = _conn(tmp_path)
    for i in range(3):
        _lesson(conn, f"a{i}", "superpowers:test-driven-development")
    called = {"n": 0}
    def runner(cmd, **kw):
        called["n"] += 1
        return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")
    out = ss.draft(conn, repo_root=tmp_path / "repo",
                   static_skill_names={"test-driven-development"},  # bare name (no prefix)
                   runner=runner, ts=TS, env=FAKE_ENV)
    assert out["skill"] is None and called["n"] == 0


def test_draft_skips_any_colon_prefixed_domain(tmp_path):
    # A ':'-prefixed domain is an existing plugin capability (skill/command/verb) the
    # eval-gate may not even enumerate → skip to avoid minting a shadowing competitor.
    conn = _conn(tmp_path)
    for i in range(3):
        _lesson(conn, f"a{i}", "ultra-memory:memory-save")
    called = {"n": 0}
    def runner(cmd, **kw):
        called["n"] += 1
        return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")
    out = ss.draft(conn, repo_root=tmp_path / "repo", static_skill_names=set(),
                   runner=runner, ts=TS, env=FAKE_ENV)
    assert out["skill"] is None and called["n"] == 0


def test_draft_does_not_skip_net_new_domain(tmp_path):
    # A domain with NO static-skill namesake (e.g. an agent's domain) IS draftable —
    # the eval-gate has no namesake to hijack, so SP-10 can mint a genuinely new skill.
    conn = _conn(tmp_path)
    for i in range(3):
        _lesson(conn, f"b{i}", "daily-market-briefing")  # an agent, not a static skill
    out = ss.draft(conn, repo_root=tmp_path / "repo",
                   static_skill_names={"backtest", "risk-manager"},
                   runner=_runner(_good_payload("gen-daily-market-briefing", ["b0", "b1", "b2"])),
                   ts=TS, env=FAKE_ENV)
    assert out["skill"] is not None and out["skill"].slug == "gen-daily-market-briefing"


def _flaky_runner(outputs):
    """Runner returning a different raw stdout per call (last value repeats)."""
    state = {"i": 0}
    def runner(cmd, **kw):
        i = state["i"]; state["i"] += 1
        return types.SimpleNamespace(returncode=0,
                                     stdout=outputs[min(i, len(outputs) - 1)], stderr="")
    return runner


def test_draft_retries_on_unparseable_json(tmp_path):
    # LLM JSON output is fragile (long markdown body → an unescaped char breaks json.loads).
    # A within-run retry recovers the common non-deterministic case → reliable autonomous yield.
    conn = _conn(tmp_path)
    for i in range(3):
        _lesson(conn, f"b{i}", "backtest")
    good = json.dumps(_good_payload("gen-backtest", ["b0", "b1", "b2"]))
    out = ss.draft(conn, repo_root=tmp_path / "repo",
                   runner=_flaky_runner(["{ not valid json ,,,", good]),  # bad → good
                   ts=TS, env=FAKE_ENV)
    assert out["skill"] is not None and out["skill"].slug == "gen-backtest"


def test_draft_gives_up_after_unparseable_attempts(tmp_path):
    # Always-malformed draft → returns skill=None (fail-soft), never raises a JSONDecodeError.
    conn = _conn(tmp_path)
    for i in range(3):
        _lesson(conn, f"b{i}", "backtest")
    out = ss.draft(conn, repo_root=tmp_path / "repo",
                   runner=_flaky_runner(["{ still bad"]), ts=TS, env=FAKE_ENV)
    assert out["skill"] is None and "unparseable" in out["reason"]


def _incumbent(conn, domain, lesson_ids):
    slug = ss.derive_slug(domain)
    memory_lib.save_memory(conn, id=ss.backing_memory_id(slug, lesson_ids),
                           type="memory", title=slug, body="marker", ts=TS,
                           index_hook=slug, node_type="generated_skill",
                           created_by="background_review")
    conn.execute(
        "INSERT OR REPLACE INTO procedures "
        "(id,name,steps,trigger,source_sessions,times_seen,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ss.procedure_id(slug), slug,
         json.dumps({"source_lesson_ids": list(lesson_ids), "fsm_state": "active"}),
         "desc", "[]", 1, TS, TS))
    conn.commit()


def test_no_delta_skips_redraft(tmp_path):
    conn = _conn(tmp_path)
    ids = ["b0", "b1", "b2"]
    for i in range(3):
        _lesson(conn, f"b{i}", "backtest")
    _incumbent(conn, "backtest", ids)
    inc = ss.active_generated_skill_for(conn, "backtest")
    assert inc is not None and set(inc["source_lesson_ids"]) == set(ids)
    assert ss.has_material_delta(inc, ids) is False
    # the draft skips the no-delta domain (no run_claude call needed)
    def _boom(cmd, **kw):
        raise AssertionError("run_claude must not be called when there is no delta")
    out = ss.draft(conn, repo_root=tmp_path / "repo", runner=_boom, ts=TS, env=FAKE_ENV)
    assert out["skill"] is None and "no eligible" in out["reason"]


def test_delta_triggers_redraft(tmp_path):
    conn = _conn(tmp_path)
    for i in range(4):                              # cluster now has 4 lessons
        _lesson(conn, f"b{i}", "backtest")
    _incumbent(conn, "backtest", ["b0", "b1", "b2"])  # built from only 3
    out = ss.draft(conn, repo_root=tmp_path / "repo",
                   runner=_runner(_good_payload("gen-backtest",
                                                ["b0", "b1", "b2", "b3"])),
                   ts=TS, env=FAKE_ENV)
    assert out["skill"] is not None
    assert out["incumbent"] is not None and out["incumbent"]["slug"] == "gen-backtest"
