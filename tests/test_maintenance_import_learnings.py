"""Tests for ultra_memory.maintenance.import_learnings — the project-agnostic
migration of the Trading self-learning driver + the Model B gen-skill block refresh.

Covers: the consumer registry seam (self_learning_files ∪ gen-* glob), the import +
projection round-trip, the SP-5 D5 data-loss fence (and its gen-* bypass), the Model
B weekly block refresh (provenance-gated, frozen frontmatter), and the learnings beat.
"""
import json
from dataclasses import dataclass
from pathlib import Path

from ultra_memory import memory_lib
from ultra_memory.maintenance import import_learnings as il
from ultra_memory.maintenance import skill_fs as sf
from ultra_memory.maintenance import skill_synthesize as ss

TS = "2026-06-02T00:00:00Z"


def _conn(tmp_path):
    return memory_lib.open_memory_db(str(tmp_path / "memory.db"))


def _learning(conn, *, id, hook, title="L", body="durable lesson", weight=1.0):
    memory_lib.save_memory(conn, id=id, type="memory", title=title, body=body,
                           ts=TS, index_hook=hook, node_type="learning",
                           created_by="background_review", created_at=TS)
    conn.execute("UPDATE memories SET outcome_weight=? WHERE id=?", (weight, id))
    conn.commit()


def _gen_on_disk(repo, slug="gen-backtest", domain="backtest", seed="### SEED\n\nseed.\n"):
    sf.create(sf.GeneratedSkill(slug=slug, description="Use when tuning backtests.",
                                body="# proc\ndo", index_hook=slug,
                                source_lesson_ids=["x"], auto_learnings_block=seed),
              repo_root=repo, ts=TS)
    return sf.skill_md_path(repo, slug)


def _procedure(conn, slug, domain):
    conn.execute(
        "INSERT OR REPLACE INTO procedures "
        "(id,name,steps,trigger,source_sessions,times_seen,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ss.procedure_id(slug), slug, json.dumps({"source_domain": domain}),
         "desc", "[]", 1, TS, TS))
    conn.commit()


@dataclass
class _Cfg:
    project_dir: Path
    briefings_dir: Path | None = None
    self_learning_files: list = None

    def __post_init__(self):
        if self.self_learning_files is None:
            self.self_learning_files = []


# --------------------------------------------------------------------------- #
# Registry seam (project-agnostic).
# --------------------------------------------------------------------------- #

def test_all_self_learning_files_unions_config_and_gen(tmp_path):
    _gen_on_disk(tmp_path, slug="gen-foo")
    reg = [(".claude/skills/backtest/Learnings.md", "backtest")]
    out = il.all_self_learning_files(tmp_path, reg)
    assert (".claude/skills/backtest/Learnings.md", "backtest") in out
    assert (".claude/skills/gen-foo/Learnings.md", "gen-foo") in out


def test_registry_default_empty_plus_gen(tmp_path):
    """No consumer registry → only the discovered generated skills (no Trading literal)."""
    _gen_on_disk(tmp_path, slug="gen-foo")
    out = il.all_self_learning_files(tmp_path, [])
    assert out == [(".claude/skills/gen-foo/Learnings.md", "gen-foo")]


# --------------------------------------------------------------------------- #
# Import + projection round-trip + the D5 data-loss fence.
# --------------------------------------------------------------------------- #

def test_import_then_regen_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    src = tmp_path / "Learnings.md"
    src.write_text("# Learnings — backtest\n\n## What has worked\n\n"
                   "- Always size with R-multiples. It anchors risk.\n")
    n = il.import_file(conn, src, skill_tag="backtest", ts=TS)
    assert n == 1
    row = conn.execute(
        "SELECT created_by, index_hook, node_type FROM memories WHERE index_hook='backtest'"
    ).fetchone()
    assert row["created_by"] == "import" and row["node_type"] == "worked"
    # now a projection regen rebuilds the file from the store
    m = il.regenerate_projection(conn, src, skill_tag="backtest")
    assert m == 1 and "Always size with R-multiples." in src.read_text()
    conn.close()


def test_regen_skipped_until_import_complete(tmp_path):
    """D5 fence: a static skill with no import done leaves its file UNTOUCHED."""
    conn = _conn(tmp_path)
    src = tmp_path / "Learnings.md"
    src.write_text("# Learnings — risk\n\nhand-written, never imported.\n")
    out = il.regenerate_projection(conn, src, skill_tag="risk-manager")
    assert out is None
    assert "hand-written, never imported." in src.read_text()
    conn.close()


def test_regen_gen_skill_bypasses_d5_fence(tmp_path):
    """A gen-* tag is a projection FROM BIRTH (no hand-written prose to lose) → it
    regenerates even with no import_complete flag."""
    conn = _conn(tmp_path)
    _learning(conn, id="g1", hook="gen-foo", title="own", body="own usage.")
    out = tmp_path / "Learnings.md"
    n = il.regenerate_projection(conn, out, skill_tag="gen-foo")
    assert n == 1 and "own usage." in out.read_text()
    conn.close()


def test_switch_refuses_until_import_complete(tmp_path):
    conn = _conn(tmp_path)
    src = tmp_path / "Learnings.md"
    src.write_text("# Learnings — x\n\nprose.\n")
    try:
        il.switch_to_projection(conn, src, skill_tag="x")
        assert False, "expected ImportIncompleteError"
    except il.ImportIncompleteError:
        pass
    conn.close()


# --------------------------------------------------------------------------- #
# Model B — refresh_generated_skill_blocks.
# --------------------------------------------------------------------------- #

def test_refresh_rewrites_gen_block_from_store(tmp_path):
    conn = _conn(tmp_path)
    md = _gen_on_disk(tmp_path, slug="gen-backtest", domain="backtest")
    _procedure(conn, "gen-backtest", "backtest")
    _learning(conn, id="d1", hook="backtest", title="DOMAIN", body="domain lesson.")
    _learning(conn, id="g1", hook="gen-backtest", title="OWN", body="own lesson.")
    n = il.refresh_generated_skill_blocks(conn, tmp_path, ts=TS)
    assert n == 1
    txt = md.read_text()
    assert "domain lesson." in txt and "own lesson." in txt
    assert "### SEED" not in txt                      # old seed replaced
    # frozen trigger: the frontmatter description is byte-preserved
    assert "Use when tuning backtests." in txt
    conn.close()


def test_refresh_uses_only_own_feed_without_procedure(tmp_path):
    """No procedures row → source_domain unknown → own-usage (gen-slug) feed only."""
    conn = _conn(tmp_path)
    md = _gen_on_disk(tmp_path, slug="gen-backtest")
    _learning(conn, id="d1", hook="backtest", title="DOMAIN", body="domain lesson.")
    _learning(conn, id="g1", hook="gen-backtest", title="OWN", body="own lesson.")
    il.refresh_generated_skill_blocks(conn, tmp_path, ts=TS)
    txt = md.read_text()
    assert "own lesson." in txt and "domain lesson." not in txt
    conn.close()


def test_refresh_skips_protected_skill(tmp_path):
    """A gen skill pinned via sp10_skill_protect:<slug> is immutable → skipped."""
    conn = _conn(tmp_path)
    md = _gen_on_disk(tmp_path, slug="gen-backtest")
    conn.execute("INSERT INTO meta (key, value) VALUES (?, '1')",
                 ("sp10_skill_protect:gen-backtest",))
    conn.commit()
    _procedure(conn, "gen-backtest", "backtest")
    _learning(conn, id="d1", hook="backtest", title="DOMAIN", body="domain lesson.")
    before = md.read_text()
    n = il.refresh_generated_skill_blocks(conn, tmp_path, ts=TS)
    assert n == 0 and md.read_text() == before
    conn.close()


def test_refresh_skips_human_frontmatter(tmp_path):
    """A SKILL.md in a gen- dir but with created_by: human is immutable to the loop."""
    conn = _conn(tmp_path)
    d = sf.skills_root(tmp_path) / "gen-hand"
    d.mkdir(parents=True)
    md = d / "SKILL.md"
    md.write_text("---\nname: gen-hand\ndescription: hand authored.\n"
                  "created_by: human\n---\n\nbody\n")
    _learning(conn, id="g1", hook="gen-hand", title="OWN", body="own lesson.")
    before = md.read_text()
    n = il.refresh_generated_skill_blocks(conn, tmp_path, ts=TS)
    assert n == 0 and md.read_text() == before
    conn.close()


# --------------------------------------------------------------------------- #
# The learnings beat.
# --------------------------------------------------------------------------- #

def test_beat_regens_standalone_and_refreshes_blocks(tmp_path):
    conn = _conn(tmp_path)
    # a static skill whose import is complete (so the D5 fence permits regen)
    skilldir = tmp_path / ".claude" / "skills" / "backtest"
    skilldir.mkdir(parents=True)
    learn = skilldir / "Learnings.md"
    learn.write_text("# Learnings — backtest\n\n## What has worked\n\n- Size with R.\n")
    il.import_file(conn, learn, skill_tag="backtest", ts=TS)
    # a generated skill on disk + a domain lesson
    md = _gen_on_disk(tmp_path, slug="gen-backtest")
    _procedure(conn, "gen-backtest", "backtest")
    _learning(conn, id="d1", hook="backtest", title="DOMAIN", body="domain lesson.")

    cfg = _Cfg(project_dir=tmp_path, briefings_dir=tmp_path / "briefings",
               self_learning_files=[(".claude/skills/backtest/Learnings.md", "backtest")])
    out = il.beat(conn, cfg, TS, {})
    assert out["blocks_refreshed"] == 1
    assert out["regenerated"] >= 1
    assert "domain lesson." in md.read_text()
    conn.close()


def test_beat_fail_open_on_missing_file(tmp_path):
    conn = _conn(tmp_path)
    cfg = _Cfg(project_dir=tmp_path,
               self_learning_files=[(".claude/skills/nope/Learnings.md", "nope")])
    out = il.beat(conn, cfg, TS, {})           # must not raise
    assert out["blocks_refreshed"] == 0
    conn.close()


# --------------------------------------------------------------------------- #
# OAuth-only / no-LLM by construction — the Tier-1 learnings beat must not even
# transitively import the LLM path (claude_cli). Runs in a clean subprocess so a
# sibling test that already loaded claude_cli does not mask the coupling.
# --------------------------------------------------------------------------- #

def test_import_learnings_does_not_import_claude_cli():
    import subprocess
    import sys
    code = (
        "import importlib, sys\n"
        "importlib.import_module('ultra_memory.maintenance.import_learnings')\n"
        "sys.exit(1 if 'ultra_memory.claude_cli' in sys.modules else 0)\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, (
        f"the no-LLM learnings beat transitively imports claude_cli "
        f"(rc={r.returncode}): {r.stderr}")


# --------------------------------------------------------------------------- #
# D5 fence edge cases (ported from Trading at parity) — the stamp-discipline that
# keeps the data-loss fence ARMED on a zero-capture-of-content-full or partial save.
# --------------------------------------------------------------------------- #

MALFORMED_CONTENT_FULL = """\
# Learnings — malformed-skill

This is a content-full file with real prose that nonetheless carries no parseable
learning structure at all: no section headings, no entry headings, no bullets.

Just paragraphs of genuine text that the parser cannot turn into learnings, so a
zero-capture here would be a SILENT data loss if it stamped the skill complete.
"""

PLACEHOLDER_ONLY = """\
# Learnings — empty-skill

---

_No learnings recorded yet._
"""

THREE_BULLETS = """\
# Learnings — partial-skill

---

## What Has Worked
- First lesson alpha. It matured into use and is durable.
- Second lesson bravo. This is the one whose save will be injected to fail.
- Third lesson charlie. It also matured and should be saved.
"""


def test_no_stamp_on_zero_capture_content_full(tmp_path):
    conn = _conn(tmp_path)
    f = tmp_path / "Learnings.md"
    f.write_text(MALFORMED_CONTENT_FULL)
    assert il.parse_learnings(MALFORMED_CONTENT_FULL) == []
    n = il.import_file(conn, f, skill_tag="malformed-skill", ts=TS)
    assert n == 0
    assert not il.import_complete(conn, "malformed-skill"), (
        "a zero-capture of a content-full file must NOT disarm the D5 fence")
    conn.close()


def test_stamps_on_genuine_placeholder(tmp_path):
    conn = _conn(tmp_path)
    f = tmp_path / "Learnings.md"
    f.write_text(PLACEHOLDER_ONLY)
    assert il.parse_learnings(PLACEHOLDER_ONLY) == []
    n = il.import_file(conn, f, skill_tag="empty-skill", ts=TS)
    assert n == 0 and il.import_complete(conn, "empty-skill")
    conn.close()


def test_no_stamp_on_partial_save_failure(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    f = tmp_path / "Learnings.md"
    f.write_text(THREE_BULLETS)
    assert len(il.parse_learnings(THREE_BULLETS)) == 3
    real_save = il.memory_lib.save_memory

    def flaky(conn_, *, id, type, title, body, ts, index_hook, node_type, created_by):
        if "bravo" in body:
            raise RuntimeError("simulated busy-retry exhaustion")
        return real_save(conn_, id=id, type=type, title=title, body=body, ts=ts,
                         index_hook=index_hook, node_type=node_type, created_by=created_by)

    monkeypatch.setattr(il.memory_lib, "save_memory", flaky)
    n = il.import_file(conn, f, skill_tag="partial-skill", ts=TS)
    assert n == 2
    assert not il.import_complete(conn, "partial-skill"), (
        "a PARTIAL import must NOT stamp complete — store is missing a row")
    conn.close()


def test_partial_retry_idempotently_fills_gap(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    f = tmp_path / "Learnings.md"
    f.write_text(THREE_BULLETS)
    real_save = il.memory_lib.save_memory
    state = {"fail": True}

    def flaky(conn_, *, id, type, title, body, ts, index_hook, node_type, created_by):
        if state["fail"] and "bravo" in body:
            raise RuntimeError("transient")
        return real_save(conn_, id=id, type=type, title=title, body=body, ts=ts,
                         index_hook=index_hook, node_type=node_type, created_by=created_by)

    monkeypatch.setattr(il.memory_lib, "save_memory", flaky)
    assert il.import_file(conn, f, skill_tag="partial-skill", ts="t1") == 2
    assert not il.import_complete(conn, "partial-skill")
    state["fail"] = False
    assert il.import_file(conn, f, skill_tag="partial-skill", ts="t2") == 3
    rows = conn.execute(
        "SELECT id FROM memories WHERE index_hook='partial-skill' AND status='active'"
    ).fetchall()
    assert len(rows) == 3                      # content-hash upsert → no duplicates
    assert il.import_complete(conn, "partial-skill")
    conn.close()


def test_full_success_stamps(tmp_path):
    conn = _conn(tmp_path)
    f = tmp_path / "Learnings.md"
    f.write_text(THREE_BULLETS)
    assert il.import_file(conn, f, skill_tag="partial-skill", ts=TS) == 3
    assert il.import_complete(conn, "partial-skill")
    conn.close()
