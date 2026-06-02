"""Generic wiki-maintenance — slice 3a: detect_stale (move-generic). Markers/caps
come from WikiSchemaConfig; the scan is a pure read-only generic algorithm.
"""
from pathlib import Path

from ultra_memory.wiki_maintenance import detect_stale as ds
from ultra_memory.wiki_maintenance import wiki_util as wu
from ultra_memory.wiki_maintenance import worklist as wl
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig


def _wiki(tmp_path):
    root = tmp_path / "wiki"
    (root / "concepts").mkdir(parents=True)
    return root


def _page(root, name, text):
    p = root / "concepts" / name
    p.write_text(text)
    return p


def _run(root, schema=None):
    w = wl.new_worklist(str(root), generated_at="2026-06-02")
    ds.run(root, w, schema=schema or WikiSchemaConfig())
    wl.finalize(w)
    return w


def _kinds(w):
    return [i["kind"] for i in w["items"]]


def test_superseded_flagged_stale_archive(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "old.md", "---\ntype: concept\ntitle: Old\nstatus: superseded\n---\n\nbody")
    w = _run(root)
    assert "stale-archive" in _kinds(w)
    assert w["items"][0]["atomic_path"] == "wiki/concepts/old.md"


def test_oversized_body_flagged_summarize(tmp_path):
    root = _wiki(tmp_path)
    schema = WikiSchemaConfig(page_soft_cap_lines=3)
    _page(root, "big.md", "---\ntype: concept\ntitle: Big\n---\n\n" + "line\n" * 10)
    w = _run(root, schema)
    assert "summarize" in _kinds(w)


def test_unresolved_conflict_flagged(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "c.md", "---\ntype: concept\ntitle: C\n---\n\n## Conflicts-with\n\nfoo")
    assert "contradiction" in _kinds(_run(root))


def test_resolved_conflict_not_flagged(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "c.md", "---\ntype: concept\ntitle: C\n---\n\n## Variant\n<!-- resolved: merged -->\nfoo")
    assert "contradiction" not in _kinds(_run(root))


def test_conflict_heading_space_variant_matches(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "c.md", "---\ntype: concept\ntitle: C\n---\n\n### Conflicts With\n\nfoo")
    assert "contradiction" in _kinds(_run(root))


def test_custom_status_field_and_marker(tmp_path):
    root = _wiki(tmp_path)
    schema = WikiSchemaConfig(status_field="lifecycle", stale_status_marker="retired")
    _page(root, "r.md", "---\ntype: concept\ntitle: R\nlifecycle: retired\n---\n\nbody")
    assert "stale-archive" in _kinds(_run(root, schema))


def test_clean_page_no_findings(tmp_path):
    root = _wiki(tmp_path)
    _page(root, "ok.md", "---\ntype: concept\ntitle: OK\n---\n\nshort clean body.")
    assert _run(root)["items"] == []


def test_rel_atomic_path_uses_wiki_root_name(tmp_path):
    root = tmp_path / "kb"           # a consumer whose wiki dir is named 'kb'
    (root / "x").mkdir(parents=True)
    f = root / "x" / "p.md"
    f.write_text("hi")
    assert wu.rel_atomic_path(f, root) == "kb/x/p.md"
