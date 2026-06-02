"""run_stage1 — the Stage-1 detector ORCHESTRATOR (project-agnostic).

Composes the 5 detectors into ONE shared worklist per wiki root, in the order their
data-dependencies require:

  1. scope   — new_atomics + non-link autofixes + recategorize/index items
  2. dedup   — cosine over precomputed vectors (needs scope's new_atomics; vectors are
               loaded by an injected `load_vecs` so the engine stays fastembed-free here)
  3. lint    — generic lint findings → broken-link autofix + worklist routing (fail-open)
  4. graph   — change-gated rebuild (consumer extractor injected) + the 3 graph queries
  5. stale   — superseded / oversized / unresolved-contradiction

Then it stamps each item's owning root (so finalize's (root, kind, atomic_path) dedup
keeps two same-basename findings from distinct roots distinct), finalizes, and writes.
`run_stage1_multi` runs this over every active root into one merged worklist. NO LLM.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from ultra_memory.wiki_maintenance import detect_dedup, detect_graph, detect_lint
from ultra_memory.wiki_maintenance import detect_scope, detect_stale, worklist
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig
from ultra_memory.wiki_maintenance.wiki_util import today_iso

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def marker_for(root) -> Path:
    """The per-root new-atomics marker: ``<root>/.last-maintenance-sha``."""
    return Path(root) / ".last-maintenance-sha"


def resolve_since_ref(wiki_root, *, explicit: str | None = None,
                      marker_path: Path | None = None) -> str:
    """Resolve the new-atomics base ref. Precedence: *explicit* verbatim → a valid SHA
    in the marker file that resolves in this repo → ``HEAD~1``."""
    if explicit is not None:
        return explicit
    if marker_path is None:
        marker_path = marker_for(wiki_root)
    try:
        raw = Path(marker_path).read_text(encoding="utf-8").strip()
    except OSError:
        return "HEAD~1"
    if not _SHA_RE.match(raw):
        if raw:
            print(f"[run_stage1] marker {marker_path} has invalid SHA {raw!r}; using HEAD~1",
                  file=sys.stderr)
        return "HEAD~1"
    try:
        rc = subprocess.run(
            ["git", "cat-file", "-e", f"{raw}^{{commit}}"],
            cwd=str(Path(wiki_root).parent), capture_output=True).returncode
    except (OSError, FileNotFoundError):
        return "HEAD~1"
    if rc != 0:
        print(f"[run_stage1] marker SHA {raw} does not resolve in repo; using HEAD~1",
              file=sys.stderr)
        return "HEAD~1"
    return raw


def run_stage1(
    wiki_root,
    out_path,
    *,
    schema: WikiSchemaConfig | None = None,
    since_ref: str | None = None,
    do_graph: bool = True,
    today: str | None = None,
    marker_path: Path | None = None,
    load_vecs=None,                       # callable(wiki_root, new_atomics) -> {path:(sha,vec)}
    graph_extractor_cmd: list[str] | None = None,
    repo_root: Path | None = None,
    lint_findings=None,                    # callable(wiki_root, schema) -> findings dict
) -> dict:
    """Compose the 5 Stage-1 detectors into one worklist for *wiki_root* and write it.
    Returns the finalized worklist dict."""
    schema = schema or WikiSchemaConfig()
    wiki_root = Path(wiki_root)
    repo_root = Path(repo_root) if repo_root is not None else wiki_root.parent
    today = today or today_iso()
    since_ref = resolve_since_ref(wiki_root, explicit=since_ref, marker_path=marker_path)
    wiki_dir = wiki_root.name

    def _resolve(p) -> Path:
        pp = Path(p)
        return pp if pp.is_absolute() else (wiki_root / pp)

    def _write_text(p, t: str) -> None:
        _resolve(p).write_text(t, encoding="utf-8")

    def _read_text(p) -> str:
        return _resolve(p).read_text(encoding="utf-8")

    w = worklist.new_worklist(str(wiki_root), generated_at=today)

    # 1. scope
    new_atomics = detect_scope.new_atomics_since(since_ref, wiki_root, schema=schema)
    detect_scope.run(wiki_root, w, new_atomic_paths=new_atomics, write_text=_write_text,
                     today=today, schema=schema)

    # 2. dedup (cached vectors only; an injected loader keeps the engine fastembed-free)
    vecs: dict = {}
    if load_vecs is not None:
        try:
            vecs = load_vecs(wiki_root, w["new_atomics"]) or {}
        except Exception as exc:  # noqa: BLE001 — fail-open
            print(f"[run_stage1] vec load failed ({exc!r}); dedup sees no vectors",
                  file=sys.stderr)
            vecs = {}
    detect_dedup.run(w, new_atomics=w["new_atomics"], vecs=vecs,
                     text_of=lambda p: (_resolve(p).read_text(encoding="utf-8")
                                        if _resolve(p).exists() else ""),
                     schema=schema)

    # 3. lint (fail-open — a routing error must never block the worklist write). A
    #    consumer may inject `lint_findings(wiki_root, schema)` to supply findings from
    #    its OWN (richer/proven) linter; absent → the engine's generic lint.
    try:
        if lint_findings is not None:
            findings = lint_findings(wiki_root, schema)
        else:
            findings = detect_lint.lint(detect_lint.collect_pages(wiki_root), schema=schema)
        rename_index = detect_lint.build_rename_index(repo_root=repo_root, wiki_subpath=wiki_dir)
        detect_lint.route_findings(findings, w, schema=schema, rename_index=rename_index,
                                   read_text=_read_text, write_text=_write_text, wiki_dir=wiki_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"[run_stage1] WARNING: lint routing failed ({exc!r}) — continuing", file=sys.stderr)

    # 4. graph
    if do_graph:
        nodes = wiki_root / "graph" / "nodes.jsonl"
        db = wiki_root / "graph" / "graph.sqlite"
        if detect_graph.needs_rebuild(wiki_root, nodes, schema=schema):
            # Substitute per-root placeholders so one extractor template serves every
            # root (e.g. ["uv","run","extract.py","{wiki_root}","--out","{graph_dir}"]).
            extractor = None
            if graph_extractor_cmd:
                extractor = [str(e).replace("{wiki_root}", str(wiki_root))
                             .replace("{graph_dir}", str(wiki_root / "graph"))
                             for e in graph_extractor_cmd]
            rc = detect_graph.rebuild(wiki_root, extractor_cmd=extractor)
            if rc not in (0, 3):
                print(f"[run_stage1] graph rebuild exited {rc} — continuing with existing graph",
                      file=sys.stderr)
        if db.exists():
            detect_graph.run_queries(db, w, schema=schema, wiki_dir=wiki_dir)

    # 5. stale
    detect_stale.run(wiki_root, w, schema=schema)

    # stamp the owning root on every item lacking one
    root_str = str(wiki_root)
    for item in w["items"]:
        if item.get("root") is None:
            item["root"] = root_str

    worklist.finalize(w)
    worklist.write_worklist(w, out_path)
    print(f"[run_stage1] counts={w['counts']}", file=sys.stderr)
    return w


def run_stage1_multi(
    out_path,
    *,
    roots: list[Path],
    schema: WikiSchemaConfig | None = None,
    since_ref: str | None = None,
    do_graph: bool = True,
    today: str | None = None,
    load_vecs=None,
    graph_extractor_cmd: list[str] | None = None,
    lint_findings=None,
) -> dict:
    """Compose Stage-1 over every root in *roots* into ONE merged worklist. The
    per-root marker is resolved via ``marker_for`` (unless *since_ref* is explicit).
    Empty *roots* → an unwritten empty-sentinel worklist (fail-soft)."""
    schema = schema or WikiSchemaConfig()
    today = today or today_iso()
    out_path = Path(out_path)

    if not roots:
        print("[run_stage1_multi] no active wiki roots — skipping", file=sys.stderr)
        return worklist.new_worklist("", generated_at=today)

    merged = worklist.new_worklist(str(roots[-1]), generated_at=today)
    for root in roots:
        root = Path(root)
        per_root_out = Path(str(out_path) + f".{root.name}.part")
        w = run_stage1(
            root, per_root_out, schema=schema, since_ref=since_ref, do_graph=do_graph,
            today=today, load_vecs=load_vecs, graph_extractor_cmd=graph_extractor_cmd,
            lint_findings=lint_findings,
            marker_path=marker_for(root) if since_ref is None else None)
        merged["items"].extend(w["items"])
        merged["new_atomics"].extend(w["new_atomics"])
        merged["auto_fixes_applied"].extend(w["auto_fixes_applied"])
        for k, v in w["graph_findings"].items():
            merged["graph_findings"].setdefault(k, []).extend(v)
        per_root_out.unlink(missing_ok=True)

    worklist.finalize(merged)
    worklist.write_worklist(merged, out_path)
    print(f"[run_stage1_multi] roots={[str(r) for r in roots]} counts={merged['counts']}",
          file=sys.stderr)
    return merged


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Stage-1 wiki-maintenance driver (generic).")
    ap.add_argument("--wiki-root", required=True, type=Path)
    ap.add_argument("--since-ref", default=None)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--no-graph", action="store_true")
    args = ap.parse_args(argv)
    run_stage1(args.wiki_root, args.out, since_ref=args.since_ref, do_graph=not args.no_graph,
               marker_path=marker_for(args.wiki_root) if args.since_ref is None else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
