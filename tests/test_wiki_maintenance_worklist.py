"""Generic wiki-maintenance — slice 2: the worklist schema (move-generic). The
KINDS taxonomy is the only schema-specific bit → configurable; everything else is
generic JSON + dedup/count logic.
"""
import pytest

from ultra_memory.wiki_maintenance import worklist as wl


def _w():
    return wl.new_worklist("wiki", generated_at="2026-06-02")


def test_new_worklist_shape():
    w = _w()
    assert w["schema_version"] == wl.SCHEMA_VERSION
    assert w["items"] == [] and w["new_atomics"] == []
    assert set(w["graph_findings"]) == {
        "contradiction_edges", "high_mention_orphans", "same_source_clusters"}


def test_add_item_appends_and_validates_kind():
    w = _w()
    wl.add_item(w, kind="cross-link", atomic_path="wiki/a.md", title="A", claim="c")
    assert w["items"][0]["kind"] == "cross-link" and w["items"][0]["priority"] == 3
    with pytest.raises(ValueError):
        wl.add_item(w, kind="bogus-kind", atomic_path="x", title="x", claim="x")


def test_add_item_accepts_custom_kinds():
    w = _w()
    wl.add_item(w, kind="my-kind", atomic_path="x", title="t", claim="c",
                kinds=("my-kind",))
    assert w["items"][0]["kind"] == "my-kind"


def test_finalize_dedups_and_counts():
    w = _w()
    wl.add_item(w, kind="summarize", atomic_path="wiki/a.md", title="A", claim="c1")
    wl.add_item(w, kind="summarize", atomic_path="wiki/a.md", title="A", claim="c2")  # dup (root,kind,path)
    wl.add_item(w, kind="cross-link", atomic_path="wiki/a.md", title="A", claim="c3")
    wl.finalize(w)
    assert len(w["items"]) == 2                       # the duplicate summarize dropped
    assert w["counts"]["by_kind"] == {"summarize": 1, "cross-link": 1}


def test_finalize_keeps_same_basename_across_roots():
    w = _w()
    wl.add_item(w, kind="summarize", atomic_path="a.md", title="A", claim="c", root="global")
    wl.add_item(w, kind="summarize", atomic_path="a.md", title="A", claim="c", root="project")
    wl.finalize(w)
    assert len(w["items"]) == 2                       # distinct roots → both kept


def test_record_autofix_and_is_empty():
    w = _w()
    assert wl.is_empty(w)
    wl.record_autofix(w, kind="fix-missing-updated", path="wiki/a.md", detail="added")
    assert not wl.is_empty(w)


def test_write_read_roundtrip(tmp_path):
    w = _w()
    wl.add_item(w, kind="contradiction", atomic_path="wiki/a.md", title="A", claim="c")
    wl.finalize(w)
    p = tmp_path / "wl.json"
    wl.write_worklist(w, p)
    assert wl.read_worklist(p)["items"][0]["kind"] == "contradiction"
