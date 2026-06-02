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


# --------------------------------------------------------------------------- #
# Stage-aware CLI (the cutover seam: separate Stage-1 detect / Stage-2 adjudicate).
# --------------------------------------------------------------------------- #

def test_stage1_build_writes_worklist(tmp_path):
    repo, root = _git_wiki(tmp_path)
    (root / "trading" / "concepts" / "lonely.md").write_text(
        "---\ntype: concept\ntitle: L\n---\nbody")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=repo, check=True)
    out = tmp_path / "wl.json"
    conn = sqlite3.connect(":memory:")
    w = wiki_curate.stage1_build(conn, _cfg(tmp_path, roots=[root]), out_path=out)
    assert out.exists() and len(w["items"]) >= 1


def test_stage1_build_no_roots_returns_none(tmp_path):
    conn = sqlite3.connect(":memory:")
    assert wiki_curate.stage1_build(conn, _cfg(tmp_path), out_path=tmp_path / "x.json") is None


def test_stage2_adjudicate_skips_without_gateway(tmp_path):
    repo, root = _git_wiki(tmp_path)
    out = tmp_path / "wl.json"
    from ultra_memory.wiki_maintenance import worklist as wl
    wl.write_worklist(wl.new_worklist(str(root), generated_at="2026-06-02"), out)
    conn = sqlite3.connect(":memory:")
    rc = wiki_curate.stage2_adjudicate(conn, _cfg(tmp_path, roots=[root], gateway=None),
                                       worklist_path=out)
    assert rc is None


def test_stage1_build_uses_injected_wiki_linter(tmp_path, monkeypatch):
    # a consumer linter declared via config.wiki_linter ("module:func") is resolved
    # (with <project_dir>/scripts on sys.path) and supplies the lint findings.
    repo, root = _git_wiki(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "my_linter.py").write_text(
        "def lint_findings(wiki_root, schema):\n"
        "    return {'orphans': [{'path': 'trading/concepts/inj.md', 'slug': 'inj'}]}\n")
    cfg = MaintenanceConfig(
        project_dir=tmp_path, db_path=tmp_path / "m.db", export_dir=tmp_path / "e",
        wiki_roots=[root], topics=["trading"], wiki_linter="my_linter:lint_findings")
    import sqlite3
    w = wiki_curate.stage1_build(sqlite3.connect(":memory:"), cfg, out_path=tmp_path / "wl.json")
    assert any(i["kind"] == "cross-link" and i["title"] == "inj" for i in w["items"])


def test_stage2_threads_injected_merge_decider(tmp_path, monkeypatch):
    # config.wiki_merge_decider ("module:func") is resolved and passed to adjudicate as
    # the grey-zone merge decider (restores the calibrated judge over the default
    # auto-merge-only). Verified by capturing adjudicate's merge_decider kwarg.
    repo, root = _git_wiki(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "my_judge.py").write_text(
        "def merge_decider(cosine, claim, cand):\n    return cosine >= 0.5\n")
    out = tmp_path / "wl.json"
    from ultra_memory.wiki_maintenance import worklist as wl
    wl.write_worklist(wl.new_worklist(str(root), generated_at="2026-06-02"), out)

    captured = {}
    from ultra_memory.wiki_maintenance import adjudicate as adj
    monkeypatch.setattr(adj, "adjudicate",
                        lambda *a, **k: captured.update(k) or 0)
    cfg = MaintenanceConfig(
        project_dir=tmp_path, db_path=tmp_path / "m.db", export_dir=tmp_path / "e",
        wiki_roots=[root], topics=["trading"], wiki_gateway=root.parent / "gw.py",
        wiki_merge_decider="my_judge:merge_decider")
    import sqlite3
    wiki_curate.stage2_adjudicate(sqlite3.connect(":memory:"), cfg, worklist_path=out)
    assert callable(captured.get("merge_decider"))
    assert captured["merge_decider"](0.6, "a", "b") is True   # the injected judge


# --------------------------------------------------------------------------- #
# M1: the beat actually CALLS _resolve_gateway and threads the prefix down.
# (The gap that let M1 pass CI: only _resolve_gateway's argv shape was unit-tested,
# never the beat call.) These integration tests assert the wiring, not just the shape.
# --------------------------------------------------------------------------- #

def _capture_adjudicate(monkeypatch):
    """Patch adj.adjudicate to record its kwargs and return rc=0."""
    captured = {}
    from ultra_memory.wiki_maintenance import adjudicate as adj
    monkeypatch.setattr(adj, "adjudicate", lambda *a, **k: captured.update(k) or 0)
    return captured


def test_stage2_adjudicate_threads_builtin_prefix_when_gateway_none(tmp_path, monkeypatch):
    """config.wiki_gateway=None → the beat resolves to the BUILT-IN turnkey prefix
    and threads it into adjudicate (not None — None gateway means skip; here we use a
    sentinel non-None spec that resolves to the built-in)."""
    repo, root = _git_wiki(tmp_path)
    out = tmp_path / "wl.json"
    from ultra_memory.wiki_maintenance import worklist as wl
    wl.write_worklist(wl.new_worklist(str(root), generated_at="2026-06-02"), out)
    captured = _capture_adjudicate(monkeypatch)
    # An empty-string gateway is "set" (not None → don't skip) but resolves to built-in.
    cfg = MaintenanceConfig(
        project_dir=tmp_path, db_path=tmp_path / "m.db", export_dir=tmp_path / "e",
        wiki_roots=[root], topics=["trading"], wiki_gateway="")
    # wiki_gateway="" is falsy-but-not-None; the beat must NOT skip on it, and must
    # route the resolved built-in prefix into adjudicate.
    import sqlite3
    rc = wiki_curate.stage2_adjudicate(sqlite3.connect(":memory:"), cfg, worklist_path=out)
    prefix = captured.get("gateway_prefix")
    assert prefix is not None, "beat did not thread a gateway_prefix into adjudicate"
    assert "ultra_memory.wiki_gateway" in " ".join(prefix)
    assert "--gateway-class" not in prefix
    assert rc == 0


def test_stage2_adjudicate_threads_gateway_class_prefix(tmp_path, monkeypatch):
    """config.wiki_gateway='module:Class' → the beat resolves to a --gateway-class
    prefix and threads it into adjudicate."""
    repo, root = _git_wiki(tmp_path)
    out = tmp_path / "wl.json"
    from ultra_memory.wiki_maintenance import worklist as wl
    wl.write_worklist(wl.new_worklist(str(root), generated_at="2026-06-02"), out)
    captured = _capture_adjudicate(monkeypatch)
    cfg = MaintenanceConfig(
        project_dir=tmp_path, db_path=tmp_path / "m.db", export_dir=tmp_path / "e",
        wiki_roots=[root], topics=["trading"],
        wiki_gateway="wiki_lib:TradingWikiGateway")
    import sqlite3
    wiki_curate.stage2_adjudicate(sqlite3.connect(":memory:"), cfg, worklist_path=out)
    prefix = captured.get("gateway_prefix")
    assert prefix is not None
    assert "--gateway-class" in prefix
    idx = prefix.index("--gateway-class")
    assert prefix[idx + 1] == "wiki_lib:TradingWikiGateway"


def test_stage2_adjudicate_threads_uv_run_prefix_for_path(tmp_path, monkeypatch):
    """A real-path gateway → the beat resolves to the back-compat uv-run prefix."""
    repo, root = _git_wiki(tmp_path)
    gw = tmp_path / "scripts" / "wiki_lib.py"
    gw.parent.mkdir(parents=True, exist_ok=True)
    gw.touch()
    out = tmp_path / "wl.json"
    from ultra_memory.wiki_maintenance import worklist as wl
    wl.write_worklist(wl.new_worklist(str(root), generated_at="2026-06-02"), out)
    captured = _capture_adjudicate(monkeypatch)
    cfg = MaintenanceConfig(
        project_dir=tmp_path, db_path=tmp_path / "m.db", export_dir=tmp_path / "e",
        wiki_roots=[root], topics=["trading"], wiki_gateway=gw)
    import sqlite3
    wiki_curate.stage2_adjudicate(sqlite3.connect(":memory:"), cfg, worklist_path=out)
    prefix = captured.get("gateway_prefix")
    assert prefix is not None
    assert prefix[0] == "uv" and "run" in prefix and str(gw) in prefix


def test_consolidate_beat_threads_resolved_prefix(tmp_path, monkeypatch):
    """consolidate.beat resolves config.wiki_gateway via _resolve_gateway and threads
    the prefix into consolidate() (the gap: the beat hardcoded uv-run instead)."""
    from ultra_memory.maintenance import consolidate as cons
    captured = {}
    monkeypatch.setattr(cons, "consolidate",
                        lambda conn, **k: captured.update(k) or {"op": "consolidate"})
    cfg = MaintenanceConfig(
        project_dir=tmp_path, db_path=tmp_path / "m.db", export_dir=tmp_path / "e",
        topics=["trading"], wiki_gateway="wiki_lib:TradingWikiGateway")
    import sqlite3
    cons.beat(sqlite3.connect(":memory:"), cfg, "2026-06-02T00:00:00Z", {})
    prefix = captured.get("gateway_prefix")
    assert prefix is not None
    assert "--gateway-class" in prefix
    assert prefix[prefix.index("--gateway-class") + 1] == "wiki_lib:TradingWikiGateway"


def test_cli_stage1(tmp_path, monkeypatch):
    repo, root = _git_wiki(tmp_path)
    (root / "trading" / "concepts" / "lonely.md").write_text(
        "---\ntype: concept\ntitle: L\n---\nbody")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=repo, check=True)
    out = tmp_path / "wl.json"
    monkeypatch.setenv("ULTRA_MEMORY_WIKI_ROOTS", str(root))
    monkeypatch.setenv("ULTRA_MEMORY_DB", str(tmp_path / "m.db"))
    rc = wiki_curate.main(["--stage", "1", "--out", str(out), "--project-dir", str(tmp_path)])
    assert rc == 0 and out.exists()
