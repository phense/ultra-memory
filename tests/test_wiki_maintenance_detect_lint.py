"""Generic wiki-maintenance — slice 4: detect_lint (split → generic core).

A self-contained, schema-driven lint over a Karpathy-style wiki: required-frontmatter
(base + per-type), size caps, broken [[wikilinks]], orphans, duplicate slugs — plus the
finding ROUTER that turns findings into worklist items / broken-link auto-fixes. The
caps + required-fm + page-types are WikiSchemaConfig seams; the worklist kinds are the
generic taxonomy.
"""
import subprocess
from pathlib import Path

from ultra_memory.wiki_maintenance import detect_lint as dl
from ultra_memory.wiki_maintenance import worklist as wl
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig


def _wiki(tmp_path):
    root = tmp_path / "wiki"
    (root / "concepts").mkdir(parents=True)
    return root


def _page(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _lint(root, schema=None):
    pages = dl.collect_pages(root)
    return dl.lint(pages, schema=schema or WikiSchemaConfig())


# --------------------------------------------------------------------------- #
# collect_pages + lint engine.
# --------------------------------------------------------------------------- #

def test_collect_pages_shape(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "concepts/a.md", "---\ntype: concept\ntitle: A\n---\n\nbody [[b]]\n")
    pages = dl.collect_pages(root)
    p = pages[0]
    assert p["path"] == "concepts/a.md" and p["slug"] == "a"
    assert p["frontmatter"]["type"] == "concept" and "b" in p["links"]


def test_lint_broken_link(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "concepts/a.md", "---\ntype: concept\ntitle: A\n---\n\nsee [[ghost]]\n")
    findings = _lint(root)
    bl = findings["broken_links"]
    assert bl and bl[0]["to"] == "ghost" and bl[0]["from_path"] == "concepts/a.md"


def test_lint_link_to_existing_page_not_broken(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "concepts/a.md", "---\ntype: concept\ntitle: A\n---\n[[b]]")
    _page(root, "concepts/b.md", "---\ntype: concept\ntitle: B\n---\nx")
    assert _lint(root)["broken_links"] == []


def test_lint_size_caps_are_schema_seams(tmp_path):
    root = _wiki(tmp_path)
    schema = WikiSchemaConfig(page_soft_cap_lines=3, page_hard_cap_lines=6)
    _page(root, "concepts/soft.md", "---\ntype: concept\ntitle: S\n---\n\n" + "x\n" * 4)
    _page(root, "concepts/hard.md", "---\ntype: concept\ntitle: H\n---\n\n" + "x\n" * 9)
    f = _lint(root, schema)
    assert [x["path"] for x in f["oversized_soft"]] == ["concepts/soft.md"]
    assert [x["path"] for x in f["oversized_hard"]] == ["concepts/hard.md"]


def test_lint_missing_required_frontmatter(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "concepts/a.md", "---\ntype: concept\n---\nbody")   # missing title
    f = _lint(root)
    mf = f["missing_frontmatter"]
    assert mf and "title" in mf[0]["missing"]


def test_lint_per_type_required_frontmatter(tmp_path):
    root = _wiki(tmp_path)
    schema = WikiSchemaConfig(type_required_fm={"mechanism": ("type", "title", "theme")})
    _page(root, "concepts/a.md", "---\ntype: mechanism\ntitle: A\n---\nbody")  # missing theme
    f = _lint(root, schema)
    assert any("theme" in x["missing"] for x in f["missing_frontmatter"])


def test_lint_malformed_frontmatter(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "concepts/a.md", "---\n: : bad\n---\nbody")
    assert _lint(root)["malformed_frontmatter"]


def test_lint_orphan_excludes_index_pages(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "concepts/lonely.md", "---\ntype: concept\ntitle: L\n---\nx")
    _page(root, "concepts/foo-index.md", "---\ntype: theme-index\ntitle: F\n---\nx")
    orphans = {o["slug"] for o in _lint(root)["orphans"]}
    assert "lonely" in orphans and "foo-index" not in orphans


def test_lint_duplicate_slugs(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "concepts/a.md", "---\ntype: concept\ntitle: A\n---\nx")
    _page(root, "extra/a.md", "---\ntype: concept\ntitle: A2\n---\ny")
    dups = _lint(root)["duplicate_slugs"]
    assert dups and dups[0]["slug"] == "a" and len(dups[0]["paths"]) == 2


# --------------------------------------------------------------------------- #
# route_findings.
# --------------------------------------------------------------------------- #

def test_route_broken_link_autofix_single_rename(tmp_path):
    w = wl.new_worklist("wiki", generated_at="2026-06-02")
    findings = {"broken_links": [{"from": "a", "from_path": "concepts/a.md", "to": "old"}]}
    store = {"concepts/a.md": "see [[old]] here"}
    dl.route_findings(
        findings, w, schema=WikiSchemaConfig(), rename_index={"old": ["new"]},
        read_text=lambda p: store[str(p)],
        write_text=lambda p, t: store.__setitem__(str(p), t), wiki_dir="wiki")
    assert "[[new]]" in store["concepts/a.md"]
    assert any(fx["kind"] == "broken-wikilink" for fx in w["auto_fixes_applied"])
    assert not [i for i in w["items"] if i["kind"] == "cross-link"]


def test_route_broken_link_no_target_to_worklist(tmp_path):
    w = wl.new_worklist("wiki", generated_at="2026-06-02")
    findings = {"broken_links": [{"from": "a", "from_path": "concepts/a.md", "to": "ghost"}]}
    dl.route_findings(
        findings, w, schema=WikiSchemaConfig(), rename_index={},
        read_text=lambda p: "see [[ghost]]", write_text=lambda p, t: None, wiki_dir="wiki")
    item = [i for i in w["items"] if i["kind"] == "cross-link"][0]
    assert item["atomic_path"] == "wiki/concepts/a.md" and item["priority"] == 2


def test_route_oversized_priorities(tmp_path):
    w = wl.new_worklist("wiki", generated_at="2026-06-02")
    findings = {"oversized_soft": [{"path": "concepts/s.md", "lines": 500}],
                "oversized_hard": [{"path": "concepts/h.md", "lines": 900}]}
    dl.route_findings(findings, w, schema=WikiSchemaConfig(), rename_index={},
                      read_text=lambda p: "", write_text=lambda p, t: None, wiki_dir="kb")
    by = {i["atomic_path"]: i["priority"] for i in w["items"]}
    assert by["kb/concepts/s.md"] == 3 and by["kb/concepts/h.md"] == 1


def test_route_wiki_dir_prefix_is_seam(tmp_path):
    w = wl.new_worklist("wiki", generated_at="2026-06-02")
    findings = {"orphans": [{"path": "concepts/o.md", "slug": "o"}]}
    dl.route_findings(findings, w, schema=WikiSchemaConfig(), rename_index={},
                      read_text=lambda p: "", write_text=lambda p, t: None, wiki_dir="kb")
    assert w["items"][0]["atomic_path"] == "kb/concepts/o.md"


# --------------------------------------------------------------------------- #
# build_rename_index + guard.
# --------------------------------------------------------------------------- #

def test_build_rename_index_parses_git_renames(tmp_path):
    repo = tmp_path
    (repo / "wiki" / "concepts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "wiki" / "concepts" / "old.md").write_text("x" * 50)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add"], cwd=repo, check=True)
    subprocess.run(["git", "mv", "wiki/concepts/old.md", "wiki/concepts/new.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "rename"], cwd=repo, check=True)
    idx = dl.build_rename_index(repo_root=repo, wiki_subpath="wiki")
    assert idx.get("old") == ["new"]


def test_no_trading_or_path_literal():
    src = Path(dl.__file__).read_text().lower()
    assert "trading" not in src and "/users/" not in src
