"""detect_graph — change-gated graph rebuild + 3 graph-health queries (split → generic).

Operates over a ``graph.sqlite`` with the documented schema:
  nodes(id, slug, title, page_type, node_type, kind, path, created, updated, metadata_json)
  edges(id, subject, predicate, object, source, evidence, confidence, status,
        extraction_method, page, metadata_json)
  aliases(alias, node_id)

Three detectors (predicates + thresholds from WikiSchemaConfig):
  1. contradiction_edges  — edges WHERE predicate = schema.graph_contradiction_predicate
  2. high_mention_orphans — nodes with > schema.graph_orphan_min_inbound inbound, 0 outbound
  3. same_source_clusters — >= schema.graph_cluster_min_subjects subjects sharing a
                            schema.graph_source_predicate object

The graph EXTRACTOR (the tool that builds graph.sqlite) is consumer-specific and is
INJECTED into ``rebuild`` as a command list — the generic engine names no extraction
script. ``run_queries`` is fully generic.
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

from ultra_memory.wiki_maintenance import worklist as wl
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig
from ultra_memory.wiki_maintenance.wiki_util import today_iso


# ---------------------------------------------------------------------------
# Change-gate.
# ---------------------------------------------------------------------------

def _newest_md_mtime(wiki_root: Path, *, ontology_file: str) -> float:
    """Max mtime among all .md files + the ontology file (0.0 if none)."""
    newest = 0.0
    for p in wiki_root.rglob("*.md"):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m > newest:
            newest = m
    ontology = wiki_root / ontology_file
    try:
        m = ontology.stat().st_mtime
        if m > newest:
            newest = m
    except OSError:
        pass
    return newest


def needs_rebuild(wiki_root, nodes_path, *,
                  schema: WikiSchemaConfig | None = None) -> bool:
    """True if *nodes_path* is missing OR older than the newest .md / ontology file."""
    schema = schema or WikiSchemaConfig()
    wiki_root = Path(wiki_root)
    nodes_path = Path(nodes_path)
    if not nodes_path.exists():
        return True
    return _newest_md_mtime(wiki_root, ontology_file=schema.graph_ontology_file) > nodes_path.stat().st_mtime


# ---------------------------------------------------------------------------
# Rebuild (extractor injected by the consumer).
# ---------------------------------------------------------------------------

def rebuild(wiki_root, *, extractor_cmd: list[str] | None,
            runner=subprocess.run) -> int:
    """Run the consumer's graph extractor command. Returns its exit code, where the
    convention is 0 = ok, 3 = sanity-gate (old graph kept, non-fatal), anything else =
    real error. ``extractor_cmd=None`` (no extractor configured) → 127, a non-(0,3)
    skip code so the orchestrator warns + continues with the existing graph rather than
    crashing. A spawn failure is swallowed to the same 127."""
    if not extractor_cmd:
        print("[detect_graph] no extractor configured — skipping rebuild", file=sys.stderr)
        return 127
    try:
        result = runner(extractor_cmd)
    except FileNotFoundError as exc:
        print(f"[detect_graph] rebuild could not spawn extractor ({extractor_cmd[0]}): {exc}",
              file=sys.stderr)
        return 127
    return result.returncode


# ---------------------------------------------------------------------------
# Queries → graph_findings + worklist items.
# ---------------------------------------------------------------------------

def run_queries(db_path, w: dict, *,
                schema: WikiSchemaConfig | None = None,
                wiki_dir: str | None = None) -> None:
    """Run the 3 detection queries against *db_path*; populate ``w['graph_findings']``
    + ``w['items']``. *wiki_dir* is the wiki root dir name used to build repo-relative
    atomic paths (default: derived from the ``<wiki_root>/graph/graph.sqlite`` layout)."""
    schema = schema or WikiSchemaConfig()
    db_path = Path(db_path)
    if wiki_dir is None:
        wiki_dir = db_path.parent.parent.name or "wiki"

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    # 1. Contradiction edges. nodes.slug stores the plain slug; edge subjects carry
    #    a namespace prefix (e.g. "mechanism:foo-bar") — strip it before joining.
    contradiction_rows = con.execute("""
        SELECT e.subject, e.object, n.path AS node_path
        FROM edges e
        LEFT JOIN nodes n
            ON n.slug = CASE
                WHEN INSTR(e.subject, ':') > 0 THEN SUBSTR(e.subject, INSTR(e.subject, ':') + 1)
                ELSE e.subject END
        WHERE e.predicate = ?
    """, (schema.graph_contradiction_predicate,)).fetchall()
    for row in contradiction_rows:
        w["graph_findings"]["contradiction_edges"].append(
            {"a": row["subject"], "b": row["object"]})
        if row["node_path"]:
            atomic_path = f"{wiki_dir}/{row['node_path']}"
        else:
            plain = row["subject"].split(":")[-1] if ":" in row["subject"] else row["subject"]
            atomic_path = f"{wiki_dir}/{schema.atomics_subdir}/{plain}.md"
        wl.add_item(
            w, kind="contradiction", atomic_path=atomic_path, title=row["subject"],
            claim=f"contradicts {row['object']}",
            evidence=f"graph edge: {row['subject']} {schema.graph_contradiction_predicate} {row['object']}",
            priority=1, kinds=schema.kinds)

    # 2. High-mention orphans: > N inbound, 0 outbound.
    orphan_rows = con.execute("""
        SELECT n.slug,
               COUNT(DISTINCT ein.id)  AS inbound,
               COUNT(DISTINCT eout.id) AS outbound
        FROM nodes n
        LEFT JOIN edges ein  ON CASE
            WHEN INSTR(ein.object, ':')  > 0 THEN SUBSTR(ein.object,  INSTR(ein.object,  ':') + 1)
            ELSE ein.object END = n.slug
        LEFT JOIN edges eout ON CASE
            WHEN INSTR(eout.subject, ':') > 0 THEN SUBSTR(eout.subject, INSTR(eout.subject, ':') + 1)
            ELSE eout.subject END = n.slug
        GROUP BY n.slug
        HAVING inbound > ? AND outbound = 0
        ORDER BY inbound DESC
    """, (schema.graph_orphan_min_inbound,)).fetchall()
    for row in orphan_rows:
        w["graph_findings"]["high_mention_orphans"].append(
            {"slug": row["slug"], "inbound": row["inbound"], "outbound": row["outbound"]})

    # 3. Same-source clusters: >= N subjects sharing a source object.
    cluster_rows = con.execute("""
        SELECT object AS source_node,
               GROUP_CONCAT(subject) AS subjects_csv,
               COUNT(DISTINCT subject) AS cnt
        FROM edges
        WHERE predicate = ?
        GROUP BY object
        HAVING cnt >= ?
        ORDER BY cnt DESC
    """, (schema.graph_source_predicate, schema.graph_cluster_min_subjects)).fetchall()
    for row in cluster_rows:
        slugs = [s.strip() for s in (row["subjects_csv"] or "").split(",") if s.strip()]
        w["graph_findings"]["same_source_clusters"].append(
            {"source": row["source_node"], "slugs": slugs})
        source_slug = row["source_node"].split(":", 1)[-1] if ":" in row["source_node"] else row["source_node"]
        wl.add_item(
            w, kind="synthesis-candidate",
            atomic_path=f"{wiki_dir}/{schema.synthesis_subdir}/{source_slug}.md",
            title=f"Cluster: {row['source_node']}",
            claim=f"{row['cnt']} atomics share source {row['source_node']}",
            evidence=f"sourced_from cluster: {', '.join(slugs[:5])}{'...' if len(slugs) > 5 else ''}",
            priority=3, kinds=schema.kinds)

    con.close()


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="detect_graph (generic wiki-maintenance).")
    ap.add_argument("--wiki-root", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    wiki_root = Path(args.wiki_root).resolve()
    db_path = wiki_root / "graph" / "graph.sqlite"
    w = wl.new_worklist(str(wiki_root), generated_at=today_iso())
    if db_path.exists():
        run_queries(db_path, w, wiki_dir=wiki_root.name)
    else:
        print(f"[detect_graph] WARNING: {db_path} not found, skipping queries", file=sys.stderr)
    wl.finalize(w)
    wl.write_worklist(w, Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
