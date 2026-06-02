"""Generic wiki-maintenance — slice 3: detect_scope (move-with-config, 15 seams).

The scope detector: new-atomics-since-ref, per-file auto-fixes (missing-updated +
anchor-collision), theme-index auto-fix + bullet routing, and missing-theme-index +
index-link detection — all TOPIC-AWARE and schema-driven. Field names, the atomics
subdir, the index-name template, the index page-types and the topic master filename
are WikiSchemaConfig seams.
"""
import subprocess
from pathlib import Path

from ultra_memory.wiki_maintenance import detect_scope as ds
from ultra_memory.wiki_maintenance import worklist as wl
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #

def _wiki(tmp_path):
    root = tmp_path / "wiki"
    root.mkdir()
    return root


def _page(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _run(root, *, new=None, schema=None):
    w = wl.new_worklist(str(root), generated_at="2026-06-02")
    writes: dict[str, str] = {}
    ds.run(root, w, new_atomic_paths=new or [], schema=schema or WikiSchemaConfig(),
           write_text=lambda p, t: writes.__setitem__(str(p), t), today="2026-06-02")
    wl.finalize(w)
    return w, writes


def _kinds(w):
    return [i["kind"] for i in w["items"]]


# --------------------------------------------------------------------------- #
# new_atomics_since — git-backed; atomics subdir is a schema seam.
# --------------------------------------------------------------------------- #

def test_new_atomics_since_detects_added_topic_concepts(tmp_path):
    repo = tmp_path
    root = _wiki(repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    _page(root, "trading/concepts/seed.md", "---\ntype: concept\ntitle: Seed\n---\nx")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    _page(root, "trading/concepts/new-one.md", "---\ntype: concept\ntitle: New\n---\ny")
    _page(root, "trading/sources/skip.md", "---\ntype: source\ntitle: S\n---\nz")  # not concepts/
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add"], cwd=repo, check=True)

    found = ds.new_atomics_since(base, root)
    assert found == ["wiki/trading/concepts/new-one.md"]


def test_new_atomics_since_respects_custom_atomics_subdir(tmp_path):
    repo = tmp_path
    root = _wiki(repo)
    schema = WikiSchemaConfig(atomics_subdir="atoms")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    _page(root, "kb/atoms/a.md", "x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    _page(root, "kb/atoms/b.md", "y")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add"], cwd=repo, check=True)

    found = ds.new_atomics_since(base, root, schema=schema)
    assert found == ["wiki/kb/atoms/b.md"]


# --------------------------------------------------------------------------- #
# Pass A — missing-updated + anchor-collision.
# --------------------------------------------------------------------------- #

def test_pass_a_adds_missing_updated(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "trading/concepts/p.md", "---\ntype: concept\ntitle: P\n---\n\nbody")
    w, writes = _run(root)
    assert any("updated-field" == fx["kind"] for fx in w["auto_fixes_applied"])
    assert any("updated: 2026-06-02" in t for t in writes.values())


def test_pass_a_anchor_collision_autofix_and_item(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "trading/concepts/a.md",
          "---\ntype: concept\ntitle: A\nanchor: dup\nupdated: 2026-06-02\n---\nx")
    _page(root, "trading/concepts/b.md",
          "---\ntype: concept\ntitle: B\nanchor: dup\nupdated: 2026-06-02\n---\ny")
    w, writes = _run(root)
    assert any(fx["kind"] == "anchor-collision" for fx in w["auto_fixes_applied"])
    assert "recategorize" in _kinds(w)
    # the rewrite touched only the SECOND (later-sorted) file's frontmatter anchor
    rewritten = [t for t in writes.values() if "anchor: dup-" in t]
    assert rewritten and all("\ny" in r for r in rewritten)


def test_pass_a_anchor_field_is_schema_seam(tmp_path):
    root = _wiki(tmp_path)
    schema = WikiSchemaConfig(anchor_field="slug_id")
    _page(root, "trading/concepts/a.md",
          "---\ntype: concept\ntitle: A\nslug_id: dup\nupdated: 2026-06-02\n---\nx")
    _page(root, "trading/concepts/b.md",
          "---\ntype: concept\ntitle: B\nslug_id: dup\nupdated: 2026-06-02\n---\ny")
    w, writes = _run(root, schema=schema)
    assert any(fx["kind"] == "anchor-collision" for fx in w["auto_fixes_applied"])
    assert any("slug_id: dup-" in t for t in writes.values())


# --------------------------------------------------------------------------- #
# Pass B — theme-index empty-section + bullet routing.
# --------------------------------------------------------------------------- #

def test_pass_b_removes_empty_autoadded_section(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "trading/concepts/foo-index.md",
          "---\ntype: theme-index\ntitle: Foo\nupdated: 2026-06-02\n---\n\n"
          "### Recently auto-added (uncategorized)\n\n\n")
    w, writes = _run(root)
    assert any(fx["kind"] == "empty-section" for fx in w["auto_fixes_applied"])


def test_pass_b_routes_uncategorized_bullets(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "trading/concepts/foo-index.md",
          "---\ntype: theme-index\ntitle: Foo\nupdated: 2026-06-02\n---\n\n"
          "### Recently auto-added (uncategorized)\n\n- **bar** a new mechanism\n")
    w, _ = _run(root)
    assert "recategorize" in _kinds(w)
    assert any("bar" in i["claim"] for i in w["items"])


# --------------------------------------------------------------------------- #
# Pass C — missing theme-index + index-link, topic-aware.
# --------------------------------------------------------------------------- #

def test_pass_c1_missing_theme_index_emits_index_create(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "trading/concepts/p.md",
          "---\ntype: concept\ntitle: P\ntheme: Macro Flow\nupdated: 2026-06-02\n---\nx")
    w, _ = _run(root)
    items = [i for i in w["items"] if i["kind"] == "index-create"]
    assert items and items[0]["theme"] == "Macro Flow"
    # topic-qualified atomic_path so the apply-resolver derives the right topic
    assert items[0]["atomic_path"] == "wiki/trading/concepts/macro-flow-index.md"


def test_pass_c1_satisfied_when_index_exists(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "trading/concepts/p.md",
          "---\ntype: concept\ntitle: P\ntheme: Macro Flow\nupdated: 2026-06-02\n---\nx")
    _page(root, "trading/concepts/macro-flow-index.md",
          "---\ntype: theme-index\ntitle: Macro Flow\nupdated: 2026-06-02\n---\n[[p]]")
    _page(root, "trading/index.md",
          "---\ntype: master-index\ntitle: Trading\nupdated: 2026-06-02\n---\n[[macro-flow-index]]")
    w, _ = _run(root)
    assert not [i for i in w["items"] if i["kind"] == "index-create"]


def test_pass_c2_unlinked_theme_index_flagged(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "trading/concepts/p.md",
          "---\ntype: concept\ntitle: P\ntheme: Macro Flow\nupdated: 2026-06-02\n---\nx")
    _page(root, "trading/concepts/macro-flow-index.md",
          "---\ntype: theme-index\ntitle: Macro Flow\nupdated: 2026-06-02\n---\n[[p]]")
    _page(root, "trading/index.md",
          "---\ntype: master-index\ntitle: Trading\nupdated: 2026-06-02\n---\n(no link)")
    w, _ = _run(root)
    assert any("not linked" in i["claim"] for i in w["items"] if i["kind"] == "recategorize")


def test_pass_c_respects_index_name_template_seam(tmp_path):
    root = _wiki(tmp_path)
    schema = WikiSchemaConfig(index_name_template="{slug}.index.md")
    _page(root, "trading/concepts/p.md",
          "---\ntype: concept\ntitle: P\ntheme: Vol\nupdated: 2026-06-02\n---\nx")
    w, _ = _run(root, schema=schema)
    items = [i for i in w["items"] if i["kind"] == "index-create"]
    assert items and items[0]["atomic_path"] == "wiki/trading/concepts/vol.index.md"


# --------------------------------------------------------------------------- #
# Portability guard.
# --------------------------------------------------------------------------- #

def test_no_trading_or_path_literal():
    src = Path(ds.__file__).read_text().lower()
    assert "trading" not in src and "/users/" not in src
