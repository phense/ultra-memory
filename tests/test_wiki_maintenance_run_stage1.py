"""Generic wiki-maintenance — slice 5: run_stage1 orchestrator. Composes the 5
detectors (scope → dedup → lint → graph → stale) into one worklist over a wiki root;
resolves the new-atomics base ref from a per-root marker; stamps each item's owning
root; merges across roots. Pure control flow — no LLM. Direct writes go through the
wiki-write-locked writer the parent shell holds (here: a tmp wiki).
"""
import subprocess
from pathlib import Path

from ultra_memory.wiki_maintenance import run_stage1 as rs
from ultra_memory.wiki_maintenance import worklist as wl
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig


def _git_wiki(tmp_path, name="wiki"):
    repo = tmp_path
    root = repo / name
    (root / "trading" / "concepts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (root / "trading" / "concepts" / "seed.md").write_text(
        "---\ntype: concept\ntitle: Seed\nupdated: 2026-06-02\n---\nx")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    return repo, root, base


def _kinds(w):
    return {i["kind"] for i in w["items"]}


# --------------------------------------------------------------------------- #
# resolve_since_ref + marker_for.
# --------------------------------------------------------------------------- #

def test_marker_for_default_location(tmp_path):
    assert rs.marker_for(tmp_path / "wiki") == tmp_path / "wiki" / ".last-maintenance-sha"


def test_resolve_since_ref_explicit_wins(tmp_path):
    assert rs.resolve_since_ref(tmp_path, explicit="abc123") == "abc123"


def test_resolve_since_ref_absent_marker_is_head1(tmp_path):
    assert rs.resolve_since_ref(tmp_path, marker_path=tmp_path / "nope") == "HEAD~1"


def test_resolve_since_ref_valid_marker(tmp_path):
    repo, root, base = _git_wiki(tmp_path)
    marker = tmp_path / "marker"
    marker.write_text(base)
    assert rs.resolve_since_ref(root, marker_path=marker) == base


def test_resolve_since_ref_invalid_marker_is_head1(tmp_path):
    repo, root, base = _git_wiki(tmp_path)
    marker = tmp_path / "marker"
    marker.write_text("not-a-sha")
    assert rs.resolve_since_ref(root, marker_path=marker) == "HEAD~1"


# --------------------------------------------------------------------------- #
# run_stage1 — composition.
# --------------------------------------------------------------------------- #

def test_composes_detectors_into_one_worklist(tmp_path):
    repo, root, base = _git_wiki(tmp_path)
    # a NEW atomic with a theme but no index → index-create
    (root / "trading" / "concepts" / "new.md").write_text(
        "---\ntype: concept\ntitle: New\ntheme: Vol\nupdated: 2026-06-02\n---\nbody")
    # a superseded page → stale-archive
    (root / "trading" / "concepts" / "old.md").write_text(
        "---\ntype: concept\ntitle: Old\nstatus: superseded\nupdated: 2026-06-02\n---\nx")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add"], cwd=repo, check=True)

    out = tmp_path / "wl.json"
    w = rs.run_stage1(root, out, since_ref=base, do_graph=False, today="2026-06-02")
    assert out.exists()
    assert "index-create" in _kinds(w) and "stale-archive" in _kinds(w)
    assert "wiki/trading/concepts/new.md" in w["new_atomics"]


def test_items_stamped_with_root(tmp_path):
    repo, root, base = _git_wiki(tmp_path)
    (root / "trading" / "concepts" / "x.md").write_text(
        "---\ntype: concept\ntitle: X\ntheme: T\nupdated: 2026-06-02\n---\nx")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add"], cwd=repo, check=True)
    w = rs.run_stage1(root, tmp_path / "wl.json", since_ref=base, do_graph=False, today="2026-06-02")
    assert w["items"] and all(i["root"] == str(root) for i in w["items"])


def test_dedup_runs_with_injected_vecs(tmp_path):
    repo, root, base = _git_wiki(tmp_path)
    (root / "trading" / "concepts" / "dup.md").write_text(
        "---\ntype: concept\ntitle: Dup\nupdated: 2026-06-02\n---\nbody")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add"], cwd=repo, check=True)

    def load_vecs(wiki_root, new_atomics):
        return {"wiki/trading/concepts/dup.md": (None, [1.0]),
                "wiki/trading/concepts/seed.md": (None, [1.0])}  # identical → cosine 1.0

    w = rs.run_stage1(root, tmp_path / "wl.json", since_ref=base, do_graph=False,
                      today="2026-06-02", load_vecs=load_vecs)
    assert "greyzone-dedup" in _kinds(w)


def test_graph_skipped_without_db(tmp_path):
    repo, root, base = _git_wiki(tmp_path)
    # do_graph=True but no graph db + no extractor → must not crash
    w = rs.run_stage1(root, tmp_path / "wl.json", since_ref=base, do_graph=True,
                      today="2026-06-02", graph_extractor_cmd=None)
    assert isinstance(w["items"], list)


# --------------------------------------------------------------------------- #
# run_stage1_multi.
# --------------------------------------------------------------------------- #

def test_multi_root_merges_and_distinguishes(tmp_path):
    repo_a, root_a, base_a = _git_wiki(tmp_path / "a", "wiki")
    repo_b, root_b, base_b = _git_wiki(tmp_path / "b", "wiki")
    for root in (root_a, root_b):
        (root / "trading" / "concepts" / "t.md").write_text(
            "---\ntype: concept\ntitle: T\ntheme: Shared\nupdated: 2026-06-02\n---\nx")

    out = tmp_path / "merged.json"
    w = rs.run_stage1_multi(out, roots=[root_a, root_b], since_ref="HEAD",
                            do_graph=False, today="2026-06-02")
    roots_seen = {i["root"] for i in w["items"]}
    assert roots_seen == {str(root_a), str(root_b)}


def test_multi_empty_roots_is_safe(tmp_path):
    out = tmp_path / "merged.json"
    w = rs.run_stage1_multi(out, roots=[], since_ref="HEAD", do_graph=False, today="2026-06-02")
    assert w["items"] == []


def test_injected_lint_findings_replaces_generic(tmp_path):
    # a consumer linter (e.g. a richer wiki_lint) supplies findings; the generic lint
    # is bypassed, and the consumer's findings are routed into the worklist.
    repo, root, base = _git_wiki(tmp_path)
    seen = {}

    def lint_findings(wiki_root, schema):
        seen["called"] = str(wiki_root)
        return {"orphans": [{"path": "trading/concepts/x.md", "slug": "x"}]}

    w = rs.run_stage1(root, tmp_path / "wl.json", since_ref=base, do_graph=False,
                      today="2026-06-02", lint_findings=lint_findings)
    assert seen["called"] == str(root)
    assert any(i["kind"] == "cross-link" and i["title"] == "x" for i in w["items"])


def test_no_trading_or_path_literal():
    src = Path(rs.__file__).read_text().lower()
    assert "trading" not in src and "/users/" not in src
