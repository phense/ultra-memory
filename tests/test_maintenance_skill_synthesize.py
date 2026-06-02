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
