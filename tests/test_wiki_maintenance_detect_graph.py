"""Generic wiki-maintenance — slice 4: detect_graph (split → generic core). The
change-gate + the 3 graph-health queries (contradiction / high-mention-orphan /
same-source-cluster) over a graph.sqlite. Edge predicates, the orphan/cluster
thresholds, the node-path prefix and the synthesis subdir are WikiSchemaConfig seams;
the graph EXTRACTOR is consumer-injected (the engine names no extraction script).
"""
import sqlite3
from pathlib import Path

from ultra_memory.wiki_maintenance import detect_graph as dg
from ultra_memory.wiki_maintenance import worklist as wl
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig


def _graph_db(tmp_path, *, nodes=(), edges=()):
    """Build a minimal graph.sqlite (the documented nodes/edges/aliases schema)."""
    root = tmp_path / "wiki"
    (root / "graph").mkdir(parents=True)
    db = root / "graph" / "graph.sqlite"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE nodes(id INTEGER PRIMARY KEY, slug TEXT, title TEXT, page_type TEXT,
                           node_type TEXT, kind TEXT, path TEXT, created TEXT, updated TEXT,
                           metadata_json TEXT);
        CREATE TABLE edges(id INTEGER PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT,
                           source TEXT, evidence TEXT, confidence REAL, status TEXT,
                           extraction_method TEXT, page TEXT, metadata_json TEXT);
        CREATE TABLE aliases(alias TEXT, node_id INTEGER);
    """)
    for slug, path in nodes:
        con.execute("INSERT INTO nodes(slug, path) VALUES (?, ?)", (slug, path))
    for subj, pred, obj in edges:
        con.execute("INSERT INTO edges(subject, predicate, object) VALUES (?, ?, ?)",
                    (subj, pred, obj))
    con.commit()
    con.close()
    return root, db


def _w():
    return wl.new_worklist("wiki", generated_at="2026-06-02")


# --------------------------------------------------------------------------- #
# Schema seams.
# --------------------------------------------------------------------------- #

def test_graph_schema_defaults():
    s = WikiSchemaConfig()
    assert s.graph_contradiction_predicate == "contradicts"
    assert s.graph_source_predicate == "sourced_from"
    assert s.graph_orphan_min_inbound == 10
    assert s.graph_cluster_min_subjects == 5
    assert s.synthesis_subdir == "synthesis"


# --------------------------------------------------------------------------- #
# run_queries.
# --------------------------------------------------------------------------- #

def test_contradiction_edge_flagged(tmp_path):
    root, db = _graph_db(
        tmp_path,
        nodes=[("foo-bar", "concepts/foo-bar.md")],
        edges=[("mechanism:foo-bar", "contradicts", "mechanism:baz")],
    )
    w = _w()
    dg.run_queries(db, w)
    assert len(w["graph_findings"]["contradiction_edges"]) == 1
    item = [i for i in w["items"] if i["kind"] == "contradiction"][0]
    assert item["priority"] == 1
    assert item["atomic_path"] == "wiki/concepts/foo-bar.md"


def test_high_mention_orphan_recorded_no_item(tmp_path):
    # 11 distinct inbound edges, 0 outbound → orphan
    edges = [(f"m:src{i}", "mentions", "m:hub") for i in range(11)]
    root, db = _graph_db(tmp_path, nodes=[("hub", "concepts/hub.md")], edges=edges)
    w = _w()
    dg.run_queries(db, w)
    assert w["graph_findings"]["high_mention_orphans"][0]["slug"] == "hub"
    assert [i for i in w["items"] if i["kind"] == "contradiction"] == []


def test_same_source_cluster_emits_synthesis_candidate(tmp_path):
    edges = [(f"m:atom{i}", "sourced_from", "source:bookX") for i in range(5)]
    root, db = _graph_db(tmp_path, edges=edges)
    w = _w()
    dg.run_queries(db, w)
    assert w["graph_findings"]["same_source_clusters"][0]["source"] == "source:bookX"
    item = [i for i in w["items"] if i["kind"] == "synthesis-candidate"][0]
    # no member/source nodes carry a path here → topic-less (single-topic) layout
    assert item["atomic_path"] == "wiki/synthesis/bookX.md"


def test_same_source_cluster_synthesis_path_carries_topic(tmp_path):
    """D10-2: in a multi-topic wiki the synthesis-candidate path must include the
    cluster's topic (derived from the source/member node paths), so create-page's
    path→topic derivation can't read the literal 'synthesis' subdir as a topic."""
    edges = [(f"m:atom{i}", "sourced_from", "source:bookX") for i in range(5)]
    nodes = [("bookX", "trading/sources/bookX.md"),
             ("atom0", "trading/concepts/atom0.md")]
    root, db = _graph_db(tmp_path, nodes=nodes, edges=edges)
    w = _w()
    dg.run_queries(db, w)
    item = [i for i in w["items"] if i["kind"] == "synthesis-candidate"][0]
    assert item["atomic_path"] == "wiki/trading/synthesis/bookX.md"


def test_same_source_cluster_topic_from_member_when_source_pathless(tmp_path):
    """Falls back to a member atomic's topic when the source node carries no path."""
    edges = [(f"m:atom{i}", "sourced_from", "source:bookX") for i in range(5)]
    nodes = [("atom3", "programming/concepts/atom3.md")]  # only a member has a path
    root, db = _graph_db(tmp_path, nodes=nodes, edges=edges)
    w = _w()
    dg.run_queries(db, w)
    item = [i for i in w["items"] if i["kind"] == "synthesis-candidate"][0]
    assert item["atomic_path"] == "wiki/programming/synthesis/bookX.md"


def test_predicate_is_schema_seam(tmp_path):
    schema = WikiSchemaConfig(graph_contradiction_predicate="conflicts_with")
    root, db = _graph_db(
        tmp_path, nodes=[("a", "concepts/a.md")],
        edges=[("m:a", "conflicts_with", "m:b")],
    )
    w = _w()
    dg.run_queries(db, w, schema=schema)
    assert len(w["graph_findings"]["contradiction_edges"]) == 1


# --------------------------------------------------------------------------- #
# needs_rebuild + rebuild (extractor injected).
# --------------------------------------------------------------------------- #

def test_needs_rebuild_missing_nodes(tmp_path):
    root = tmp_path / "wiki"
    (root).mkdir()
    assert dg.needs_rebuild(root, root / "graph" / "nodes.jsonl") is True


def test_rebuild_runs_injected_extractor(tmp_path):
    root = tmp_path / "wiki"
    root.mkdir()
    calls = {}

    def fake_runner(cmd, **kw):
        calls["cmd"] = cmd
        class R:  # noqa: D401
            returncode = 0
        return R()

    rc = dg.rebuild(root, extractor_cmd=["my-extractor", str(root)], runner=fake_runner)
    assert rc == 0 and calls["cmd"][0] == "my-extractor"


def test_rebuild_without_extractor_skips(tmp_path):
    root = tmp_path / "wiki"
    root.mkdir()
    # No extractor configured → a non-(0,3) skip code, never a crash.
    rc = dg.rebuild(root, extractor_cmd=None)
    assert rc not in (0, 3)


def test_no_trading_or_path_literal():
    src = Path(dg.__file__).read_text().lower()
    assert "trading" not in src and "/users/" not in src
