"""Generic wiki-maintenance — slice 6: the chained `wiki_maintenance` beat. Builds the
Stage-1 worklist over config.wiki_roots (load_wiki_schema seam), then runs the Stage-2
adjudicate apply through the consumer gateway. Self-gates to a no-op when no wiki_roots
are set (pure-memory install). Registered + gated by run_pipeline.
"""
import sqlite3
import subprocess
from pathlib import Path

from ultra_memory.maintenance import wiki_curate
from ultra_memory.maintenance.config import MaintenanceConfig
from ultra_memory.maintenance.run import default_registry, BEAT_ORDER


def _cfg(tmp_path, *, roots=(), gateway=None):
    return MaintenanceConfig(
        project_dir=tmp_path, db_path=tmp_path / "m.db",
        export_dir=tmp_path / "export", wiki_roots=list(roots),
        wiki_gateway=gateway, topics=["trading"])


def _git_wiki(tmp_path):
    repo = tmp_path / "proj"
    root = repo / "wiki"
    (root / "trading" / "concepts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    return repo, root


def test_beat_no_wiki_roots_is_noop(tmp_path):
    conn = sqlite3.connect(":memory:")
    res = wiki_curate.beat(conn, _cfg(tmp_path), "2026-06-02T00:00:00Z", {})
    assert res["skipped"] == "no-wiki-roots"


def test_beat_stage1_only_without_gateway(tmp_path):
    repo, root = _git_wiki(tmp_path)
    # an orphan concept page → a Stage-1 finding, but NO gateway → no adjudicate/LLM
    (root / "trading" / "concepts" / "lonely.md").write_text(
        "---\ntype: concept\ntitle: L\n---\nbody")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=repo, check=True)

    conn = sqlite3.connect(":memory:")
    res = wiki_curate.beat(conn, _cfg(tmp_path, roots=[root], gateway=None),
                           "2026-06-02T00:00:00Z", {})
    assert res["stage1_items"] >= 1 and res["adjudicated"] is False


def test_beat_full_chain_clean_wiki_completes(tmp_path):
    repo, root = _git_wiki(tmp_path)
    # two mutually-linking pages → no orphans, full frontmatter, no theme → ZERO
    # Stage-1 items → adjudicate's skip-if-empty path → no LLM spawned.
    (root / "trading" / "concepts" / "a.md").write_text(
        "---\ntype: concept\ntitle: A\nupdated: 2026-06-02\n---\nsee [[b]]")
    (root / "trading" / "concepts" / "b.md").write_text(
        "---\ntype: concept\ntitle: B\nupdated: 2026-06-02\n---\nsee [[a]]")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=repo, check=True)

    conn = sqlite3.connect(":memory:")
    # gateway set, but a clean wiki yields no actionable items → adjudicate skips,
    # ZERO LLM calls — so the full chain completes without spawning claude.
    res = wiki_curate.beat(conn, _cfg(tmp_path, roots=[root], gateway=root.parent / "gw.py"),
                           "2026-06-02T00:00:00Z", {})
    assert res["adjudicated"] is True and res["adjudicate_rc"] == 0


def test_beat_registered_and_ordered():
    assert "wiki_maintenance" in default_registry()
    assert "wiki_maintenance" in BEAT_ORDER
