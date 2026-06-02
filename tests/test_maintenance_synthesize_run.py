"""Tests for synthesize_run.py — SP-10 Stage 5b orchestrator (incl. fork-H supersede)."""
import json
import sys
import types
from pathlib import Path

import pytest


from ultra_memory.maintenance import synthesize_run as sr  # noqa: E402
from ultra_memory.maintenance import skill_synthesize as ss  # noqa: E402
from ultra_memory.maintenance import skill_fs as sf  # noqa: E402
from ultra_memory import memory_lib  # noqa: E402

TS = "2026-06-01T00:00:00Z"
DATE = "2026-06-01"
FAKE_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": "tok-test"}
STATICS = {"risk-manager": "MUST use before any trading action, placing an order."}
CORPUS = [{"query": "size my SPY put position before I trade", "expect": "risk-manager",
           "should_trigger": True}]


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("SP10_SYNTHESIS_DISABLE", raising=False)
    monkeypatch.delenv("SP10_SYNTHESIS_DRYRUN", raising=False)


def _conn(tmp_path):
    return memory_lib.open_memory_db(str(tmp_path / "memory.db"))


def _lessons(conn, n, domain="backtest"):
    for i in range(n):
        memory_lib.save_memory(conn, id=f"b{i}", type="learning", title="L",
                               body="durable lesson", ts=TS, index_hook=domain,
                               node_type="learning", created_by="background_review")


def _payload(slug, ids):
    return {"skill": {"name": slug, "description": "Use when tuning the vol calendar.",
                      "body": "# proc\ndo", "paths": ["scripts/**"],
                      "source_lesson_ids": list(ids)}}


def _runner(payload):
    def runner(cmd, **kw):
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    return runner


def _ckpt_ok(date):
    return types.SimpleNamespace(ok=True, reason=None, tag="t",
                                 rollback_command="git reset --soft t")


def _run(conn, tmp_path, **over):
    kw = dict(repo_root=tmp_path / "repo", date=DATE, ts=TS,
              briefings_dir=tmp_path / "briefings", env=FAKE_ENV,
              static_descriptions=STATICS, corpus=CORPUS,
              probe_fn=lambda q, c: False, checkpoint_fn=_ckpt_ok)
    kw.update(over)
    return sr.run_synthesize_pass(conn, **kw)


def test_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SP10_SYNTHESIS_DISABLE", "1")
    conn = _conn(tmp_path)
    _lessons(conn, 3)
    res = _run(conn, tmp_path, runner=_runner(_payload("gen-backtest", ["b0", "b1", "b2"])))
    assert res.mode == "noop" and res.applied is None
    assert not sf.skill_md_path(tmp_path / "repo", "gen-backtest").exists()
    assert Path(res.digest_path).exists()


def test_dryrun_admits_but_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setenv("SP10_SYNTHESIS_DRYRUN", "1")
    conn = _conn(tmp_path)
    _lessons(conn, 3)
    res = _run(conn, tmp_path, runner=_runner(_payload("gen-backtest", ["b0", "b1", "b2"])))
    assert res.mode == "dryrun" and res.admitted == "gen-backtest" and res.applied is None
    assert not sf.skill_md_path(tmp_path / "repo", "gen-backtest").exists()


def test_live_apply_writes_dual_representation(tmp_path):
    conn = _conn(tmp_path)
    _lessons(conn, 3)
    res = _run(conn, tmp_path, runner=_runner(_payload("gen-backtest", ["b0", "b1", "b2"])))
    assert res.mode == "live" and res.applied == "gen-backtest"
    md = sf.skill_md_path(tmp_path / "repo", "gen-backtest")
    assert md.is_file() and "background_review" in md.read_text()
    # backing memory row + procedures ledger + synthesized_into edges
    new_id = ss.backing_memory_id("gen-backtest", ["b0", "b1", "b2"])
    row = conn.execute("SELECT node_type, status, index_hook FROM memories WHERE id=?",
                       (new_id,)).fetchone()
    assert row["node_type"] == "generated_skill" and row["status"] == "active"
    proc = conn.execute("SELECT name, times_seen FROM procedures WHERE id=?",
                        (ss.procedure_id("gen-backtest"),)).fetchone()
    assert proc["name"] == "gen-backtest" and proc["times_seen"] == 1
    edges = conn.execute(
        "SELECT COUNT(*) c FROM links WHERE predicate='synthesized_into' AND dst_id=?",
        (new_id,)).fetchone()
    assert edges["c"] == 3


def test_live_reject_on_hijack_writes_nothing(tmp_path):
    conn = _conn(tmp_path)
    _lessons(conn, 3)
    res = _run(conn, tmp_path,
               runner=_runner(_payload("gen-backtest", ["b0", "b1", "b2"])),
               probe_fn=lambda q, c: True)  # candidate steals the risk-manager probe
    assert res.applied is None and res.verdict == "reject"
    assert res.rejected and not sf.skill_md_path(tmp_path / "repo", "gen-backtest").exists()


def test_supersede_archives_incumbent(tmp_path):
    conn = _conn(tmp_path)
    _lessons(conn, 4)  # cluster has 4 lessons now
    # seed an incumbent built from only 3 lessons (a real on-disk skill + dual rep)
    inc_skill = sf.GeneratedSkill(slug="gen-backtest", description="old desc",
                                  body="old", index_hook="gen-backtest",
                                  source_lesson_ids=["b0", "b1", "b2"])
    sf.create(inc_skill, repo_root=tmp_path / "repo", ts=TS)
    inc_mem = ss.backing_memory_id("gen-backtest", ["b0", "b1", "b2"])
    memory_lib.save_memory(conn, id=inc_mem, type="memory", title="gen-backtest",
                           body="marker", ts=TS, index_hook="gen-backtest",
                           node_type="generated_skill", created_by="background_review")
    conn.execute("INSERT OR REPLACE INTO procedures "
                 "(id,name,steps,trigger,source_sessions,times_seen,created_at,updated_at)"
                 " VALUES (?,?,?,?,?,?,?,?)",
                 (ss.procedure_id("gen-backtest"), "gen-backtest",
                  json.dumps({"source_lesson_ids": ["b0", "b1", "b2"], "fsm_state": "active"}),
                  "old desc", "[]", 1, TS, TS))
    conn.commit()

    res = _run(conn, tmp_path,
               runner=_runner(_payload("gen-backtest", ["b0", "b1", "b2", "b3"])))
    assert res.applied == "gen-backtest" and res.superseded == inc_mem
    # incumbent backing memory retired (redirect, out of recall); new one active
    old = conn.execute("SELECT status FROM memories WHERE id=?", (inc_mem,)).fetchone()
    assert old["status"] == "redirect"
    new_id = ss.backing_memory_id("gen-backtest", ["b0", "b1", "b2", "b3"])
    assert conn.execute("SELECT status FROM memories WHERE id=?", (new_id,)).fetchone()["status"] == "active"
    # superseded_by edge + per-domain uniqueness (≤1 active) + archived dir exists
    assert conn.execute("SELECT COUNT(*) c FROM links WHERE predicate='superseded_by' "
                        "AND src_id=? AND dst_id=?", (inc_mem, new_id)).fetchone()["c"] == 1
    inc = ss.active_generated_skill_for(conn, "backtest")
    assert inc["mem_id"] == new_id
    assert (sf.archive_root(tmp_path / "repo") / "gen-backtest").exists()
    assert conn.execute("SELECT times_seen FROM procedures WHERE id=?",
                        (ss.procedure_id("gen-backtest"),)).fetchone()["times_seen"] == 2


def test_halt_on_pinned_source(tmp_path):
    conn = _conn(tmp_path)
    _lessons(conn, 3)
    memory_lib.set_pinned(conn, id="b2", pinned=True, ts=TS, reason="t")
    res = _run(conn, tmp_path, runner=_runner(_payload("gen-backtest", ["b0", "b1", "b2"])))
    assert res.halted is True and res.applied is None


def test_fail_open_on_bad_corpus(tmp_path):
    conn = _conn(tmp_path)
    _lessons(conn, 3)
    res = sr.run_synthesize_pass(
        conn, repo_root=tmp_path / "repo", date=DATE, ts=TS,
        briefings_dir=tmp_path / "briefings", env=FAKE_ENV,
        static_descriptions=STATICS, corpus=None,
        corpus_path=str(tmp_path / "nope.json"),  # missing → load_corpus raises
        runner=_runner(_payload("gen-backtest", ["b0", "b1", "b2"])),
        probe_fn=lambda q, c: False, checkpoint_fn=_ckpt_ok)
    assert res.applied is None and "fail-open" in res.reason
    assert Path(res.digest_path).exists()  # still wrote a digest


def _seed_incumbent(conn, repo, ids, *, slug="gen-backtest", source_domain="backtest",
                    created_at=TS, on_disk=True):
    if on_disk:
        sf.create(sf.GeneratedSkill(slug=slug, description="old", body="old",
                                    index_hook=slug, source_lesson_ids=ids),
                  repo_root=repo, ts=TS)
    mem = ss.backing_memory_id(slug, ids)
    memory_lib.save_memory(conn, id=mem, type="memory", title=slug, body="marker",
                           ts=TS, index_hook=slug, node_type="generated_skill",
                           created_by="background_review")
    conn.execute("INSERT OR REPLACE INTO procedures "
                 "(id,name,steps,trigger,source_sessions,times_seen,created_at,updated_at)"
                 " VALUES (?,?,?,?,?,?,?,?)",
                 (ss.procedure_id(slug), slug,
                  json.dumps({"source_lesson_ids": ids, "fsm_state": "active",
                              "source_domain": source_domain, "created_at": created_at}),
                  "old", "[]", 1, created_at, TS))
    conn.commit()
    return mem


def test_subset_citation_does_not_self_redirect(tmp_path):
    # BLOCKER regression (2026-06-01): cluster grows to {b0..b3} (delta=True) but the
    # model cites only the incumbent's set {b0,b1,b2}. Identity now keys on the FULL
    # cluster, so no id collision / self-redirect — exactly one active backing row.
    conn = _conn(tmp_path)
    _lessons(conn, 4)
    inc = _seed_incumbent(conn, tmp_path / "repo", ["b0", "b1", "b2"])
    res = _run(conn, tmp_path,
               runner=_runner(_payload("gen-backtest", ["b0", "b1", "b2"])))  # SUBSET citation
    assert res.applied == "gen-backtest"
    active = conn.execute("SELECT id FROM memories WHERE node_type='generated_skill' "
                          "AND index_hook='gen-backtest' AND status='active'").fetchall()
    assert len(active) == 1                                   # the invariant holds
    new_id = ss.backing_memory_id("gen-backtest", ["b0", "b1", "b2", "b3"])  # = cluster ids
    assert active[0]["id"] == new_id and res.superseded == inc
    # no self-loop superseded_by edge
    assert conn.execute("SELECT COUNT(*) c FROM links WHERE predicate='superseded_by' "
                        "AND src_id=dst_id").fetchone()["c"] == 0


def test_supersede_tolerates_drift_missing_dir(tmp_path):
    conn = _conn(tmp_path)
    _lessons(conn, 4)
    inc = _seed_incumbent(conn, tmp_path / "repo", ["b0", "b1", "b2"], on_disk=False)
    assert not sf.skill_dir(tmp_path / "repo", "gen-backtest").exists()  # drift
    res = _run(conn, tmp_path,
               runner=_runner(_payload("gen-backtest", ["b0", "b1", "b2", "b3"])))
    assert res.applied == "gen-backtest" and res.superseded == inc   # not wedged
    assert conn.execute("SELECT status FROM memories WHERE id=?", (inc,)).fetchone()["status"] == "redirect"


def test_cross_domain_slug_collision_skipped(tmp_path):
    conn = _conn(tmp_path)
    # two domains slugify to the same gen-foo-bar; incumbent belongs to 'foo-bar'.
    for i in range(3):
        memory_lib.save_memory(conn, id=f"x{i}", type="learning", title="L",
                               body="lesson", ts=TS, index_hook="foo bar",
                               node_type="learning", created_by="background_review")
    _seed_incumbent(conn, tmp_path / "repo", ["y0", "y1", "y2"], slug="gen-foo-bar",
                    source_domain="foo-bar")
    res = _run(conn, tmp_path,
               runner=_runner(_payload("gen-foo-bar", ["x0", "x1", "x2"])))
    assert res.applied is None  # the colliding 'foo bar' domain is skipped, never cross-supersede


def test_period_cap_blocks_stacked_runs(tmp_path):
    from ultra_memory.maintenance import synthesize_bounds as sb
    conn = _conn(tmp_path)
    _lessons(conn, 3)
    sb.commit_period_usage(conn, period="2026-06", applied_count=sb.MAX_SKILLS_INDUCED_PER_PERIOD)
    res = _run(conn, tmp_path, period="2026-06",
               runner=_runner(_payload("gen-backtest", ["b0", "b1", "b2"])))
    assert res.applied is None and "period" in str(res.reason)


def test_supersede_preserves_created_at(tmp_path):
    conn = _conn(tmp_path)
    _lessons(conn, 4)
    _seed_incumbent(conn, tmp_path / "repo", ["b0", "b1", "b2"], created_at="2020-01-01T00:00:00Z")
    _run(conn, tmp_path, runner=_runner(_payload("gen-backtest", ["b0", "b1", "b2", "b3"])))
    ca = conn.execute("SELECT created_at FROM procedures WHERE id=?",
                      (ss.procedure_id("gen-backtest"),)).fetchone()["created_at"]
    assert ca == "2020-01-01T00:00:00Z"   # first-induction date not clobbered on redraft


def test_checkpoint_uses_sp10_tag(tmp_path):
    # the live (non-stubbed) checkpoint must stamp pre-sp10-synthesize-<date>, NOT the
    # SP-7 default — else a same-day SP-7 run clobbers the SP-10 rollback anchor.
    from ultra_memory.maintenance.aggressive_bounds import pre_run_checkpoint
    r = pre_run_checkpoint(repo_root=tmp_path, date="2026-06-01",
                           export_fn=lambda: None, tag_prefix="pre-sp10-synthesize-")
    assert r.tag == "pre-sp10-synthesize-2026-06-01"
    assert "pre-sp10-synthesize-2026-06-01" in r.rollback_command


def test_checkpoint_dirty_tree_plan_only(tmp_path):
    conn = _conn(tmp_path)
    _lessons(conn, 3)
    dirty = lambda date: types.SimpleNamespace(ok=False, reason="dirty tree",
                                               rollback_command="rb")
    res = _run(conn, tmp_path,
               runner=_runner(_payload("gen-backtest", ["b0", "b1", "b2"])),
               checkpoint_fn=dirty)
    assert res.applied is None and "checkpoint not ok" in res.reason
    assert not sf.skill_md_path(tmp_path / "repo", "gen-backtest").exists()


def test_live_apply_seeds_auto_learnings_block(tmp_path):
    """Model B: the SKILL.md is written WITH the managed block SEEDED from the
    founding cluster lessons (so it is substantive on day 1, never relies on a
    pre-existing Learnings.md). Same union-blend renderer as the weekly refresh."""
    conn = _conn(tmp_path)
    _lessons(conn, 3)
    res = _run(conn, tmp_path, runner=_runner(_payload("gen-backtest", ["b0", "b1", "b2"])))
    assert res.applied == "gen-backtest"
    txt = sf.skill_md_path(tmp_path / "repo", "gen-backtest").read_text()
    assert sf.AUTO_BEGIN in txt and sf.AUTO_END in txt
    # the founding cluster lesson bodies seed the block region
    assert "durable lesson" in txt[txt.index(sf.AUTO_BEGIN):]
    # frozen trigger: the drafted description survives verbatim in the frontmatter
    assert "Use when tuning the vol calendar." in txt
