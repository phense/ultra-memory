"""The `wiki_maintenance` beat — the consumer-config bridge for the generic
wiki-maintenance pipeline (Stage-1 detect → Stage-2 adjudicate), wired into the Tier-2
orchestrator.

It maps MaintenanceConfig onto the project-agnostic ``wiki_maintenance`` package:
  * the wiki schema from ``config.wiki_schema`` (load_wiki_schema);
  * the roots from ``config.wiki_roots`` (existing dirs only) → ``run_stage1_multi``;
  * atomic vectors loaded via the engine's ``retrieval_core`` cache (fastembed-soft;
    short-circuited when there are no new atomics — the only time dedup needs them);
  * the graph extractor command from ``config.wiki_graph_extractor`` (per-root templated);
  * the Stage-2 decision applied through the consumer gateway ``config.wiki_gateway``.

Self-gating: no ``wiki_roots`` → a no-op (a pure-memory install is unaffected). No
``wiki_gateway`` → Stage-1 only (detect, no LLM, no writes). OAuth-only Stage-2 (the
default ``adjudicate`` claude_call is ``run_claude``). Fail-open: any error is caught by
the orchestrator and recorded — this beat itself never raises out.
"""
from __future__ import annotations

from pathlib import Path

from ultra_memory.wiki_maintenance import adjudicate as adj
from ultra_memory.wiki_maintenance import run_stage1 as rs
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig, load_wiki_schema
from ultra_memory.wiki_maintenance.wiki_util import rel_atomic_path, split_frontmatter, wiki_md_files


def _active_roots(config) -> list[Path]:
    return [Path(r) for r in config.wiki_roots if Path(r).is_dir()]


def _atomic_pages(wiki_root: Path, schema: WikiSchemaConfig) -> list[Path]:
    """Every atomic page (a file directly under an ``atomics_subdir``), index pages
    excluded — the dedup corpus."""
    suffix = schema.index_name_template.format(slug="")
    out = []
    for f in wiki_md_files(wiki_root):
        if f.parent.name == schema.atomics_subdir and not f.name.endswith(suffix):
            out.append(f)
    return out


def _make_vec_loader(conn, schema: WikiSchemaConfig):
    """A load_vecs(wiki_root, new_atomics) → {atomic_path: (None, vec)} backed by the
    engine's embedding cache. Short-circuits to {} when there are no new atomics (the
    only case dedup needs vectors) and fail-opens to {} if embedding is unavailable."""
    def load_vecs(wiki_root, new_atomics):
        if not new_atomics:
            return {}
        try:
            from ultra_memory import retrieval_core
            embedder = retrieval_core.default_embedder(schema.embed_model)
        except Exception:
            return {}
        items = []
        for f in _atomic_pages(Path(wiki_root), schema):
            try:
                _, _, body = split_frontmatter(f.read_text(encoding="utf-8"))
            except OSError:
                continue
            items.append(("wiki_atomic", rel_atomic_path(f, wiki_root), body))
        if not items:
            return {}
        try:
            vecs = retrieval_core.get_or_embed_batch(
                conn, items, embedder=embedder, model_name=schema.embed_model)
        except Exception:
            return {}
        return {tid: (None, vec) for tid, vec in vecs.items()}
    return load_vecs


def _resolve_linter(config):
    """Resolve config.wiki_linter ("module:function") to a callable, with the project
    dir + its scripts/ on sys.path so a consumer's in-tree linter is importable. Empty
    / unresolvable → None (the engine's generic lint). Fail-open."""
    import importlib
    import sys

    spec = (getattr(config, "wiki_linter", "") or "").strip()
    if not spec or ":" not in spec:
        return None
    mod_name, _, fn_name = spec.partition(":")
    if not mod_name or not fn_name:
        return None
    for p in (str(config.project_dir / "scripts"), str(config.project_dir)):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        return getattr(importlib.import_module(mod_name), fn_name)
    except Exception as exc:  # noqa: BLE001 — a bad hook must never wedge maintenance
        print(f"[wiki_curate] could not resolve wiki_linter {spec!r}: {exc!r} — "
              f"using the generic lint", file=__import__("sys").stderr)
        return None


def _collect_index_stems(roots: list[Path], schema: WikiSchemaConfig) -> list[str]:
    """The CURRENT theme-index stems across the roots — the content-free link-hygiene
    example for the Stage-2 system prompt (no consumer literal)."""
    suffix = schema.index_name_template.format(slug="")
    index_types = set(schema.index_types)
    stems: set[str] = set()
    for root in roots:
        for f in wiki_md_files(root):
            if not f.name.endswith(suffix):
                continue
            try:
                fm, _, _ = split_frontmatter(f.read_text(encoding="utf-8"))
            except OSError:
                continue
            if fm.get(schema.type_field) in index_types:
                stems.add(f.stem)
    return sorted(stems)


def stage1_build(conn, config, *, out_path, schema=None):
    """Stage-1: build the worklist over the active roots and write it to *out_path*.
    Returns the worklist dict, or None when there are no active wiki roots (no-op)."""
    roots = _active_roots(config)
    if not roots:
        return None
    schema = schema or load_wiki_schema(config.wiki_schema)
    extractor = list(config.wiki_graph_extractor) if config.wiki_graph_extractor else None
    load_vecs = _make_vec_loader(conn, schema)
    return rs.run_stage1_multi(
        out_path, roots=roots, schema=schema, do_graph=True,
        load_vecs=load_vecs, graph_extractor_cmd=extractor,
        lint_findings=_resolve_linter(config))


def stage2_adjudicate(conn, config, *, worklist_path, schema=None, env=None):
    """Stage-2: run the OAuth adjudication over the worklist via the consumer gateway.
    Returns the adjudicate exit code, or None when there is no root / no gateway."""
    roots = _active_roots(config)
    if not roots or config.wiki_gateway is None:
        return None
    schema = schema or load_wiki_schema(config.wiki_schema)
    default_topic = config.topics[0] if config.topics else "default"
    return adj.adjudicate(
        worklist_path, gateway=config.wiki_gateway, model=config.model, schema=schema,
        env=env, default_topic=default_topic, wiki_dir=roots[-1].name or "wiki",
        index_stems=_collect_index_stems(roots, schema), fallback_cwd=roots[-1].parent)


def beat(conn, config, ts, env):
    """The `wiki_maintenance` Tier-2 beat (Stage-1 → Stage-2 in one call, the worklist
    staged under the export dir). Returns a summary dict (recorded by the orchestrator).
    NEVER raises out — each phase already fail-opens internally."""
    roots = _active_roots(config)
    if not roots:
        return {"skipped": "no-wiki-roots"}

    schema = load_wiki_schema(config.wiki_schema)
    out_dir = Path(config.export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "wiki-maintenance-worklist.json"

    w = stage1_build(conn, config, out_path=out_path, schema=schema)
    result = {
        "roots": [str(r) for r in roots],
        "stage1_items": len(w.get("items", [])),
        "stage1_autofixes": len(w.get("auto_fixes_applied", [])),
        "adjudicated": False,
        "adjudicate_rc": None,
    }
    if config.wiki_gateway is None:
        result["skipped_stage2"] = "no-gateway"
        return result
    rc = stage2_adjudicate(conn, config, worklist_path=out_path, schema=schema, env=env)
    result["adjudicated"] = True
    result["adjudicate_rc"] = rc
    return result


def main(argv=None) -> int:
    """Stage-aware CLI (the cutover seam): a consumer's shell pipeline calls Stage-1 and
    Stage-2 as separate, individually-timed steps with a worklist-on-disk handoff —
    byte-for-byte the two-stage structure the in-tree scripts had.

        python -m ultra_memory.maintenance.wiki_curate --stage 1 --out <worklist>
        python -m ultra_memory.maintenance.wiki_curate --stage 2 --worklist <worklist>
        python -m ultra_memory.maintenance.wiki_curate --stage all   # the beat (both)
    """
    import argparse
    import os
    import sys

    from ultra_memory import memory_lib
    from ultra_memory.maintenance.config import load_config

    ap = argparse.ArgumentParser(prog="python -m ultra_memory.maintenance.wiki_curate",
                                 description="Stage-aware wiki-maintenance (Stage-1 detect / Stage-2 adjudicate).")
    ap.add_argument("--stage", choices=("1", "2", "all"), default="all")
    ap.add_argument("--out", default=None, help="Stage-1 worklist output path.")
    ap.add_argument("--worklist", default=None, help="Stage-2 worklist input path.")
    ap.add_argument("--project-dir", default=None)
    args = ap.parse_args(argv)

    config = load_config(project_dir=args.project_dir, env=os.environ)
    try:
        conn = memory_lib.open_memory_db(str(config.db_path))
    except Exception as exc:  # fail-open: a missing/locked DB must never wedge the cron
        sys.stderr.write(f"[wiki_curate] cannot open DB {config.db_path}: {exc!r} — skipping\n")
        return 0
    try:
        if args.stage == "1":
            if not args.out:
                ap.error("--stage 1 requires --out")
            w = stage1_build(conn, config, out_path=args.out)
            sys.stderr.write(f"[wiki_curate] stage1 items={0 if w is None else len(w['items'])}\n")
        elif args.stage == "2":
            wl_path = args.worklist or args.out
            if not wl_path:
                ap.error("--stage 2 requires --worklist")
            rc = stage2_adjudicate(conn, config, worklist_path=wl_path, env=os.environ)
            sys.stderr.write(f"[wiki_curate] stage2 rc={rc}\n")
            return rc or 0
        else:
            res = beat(conn, config, None, os.environ)
            sys.stderr.write(f"[wiki_curate] {res}\n")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
