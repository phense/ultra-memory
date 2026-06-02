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


def beat(conn, config, ts, env):
    """The `wiki_maintenance` Tier-2 beat. Returns a summary dict (recorded by the
    orchestrator). NEVER raises out — the orchestrator's fail-open wrapper relies on it,
    and each phase already fail-opens internally."""
    roots = _active_roots(config)
    if not roots:
        return {"skipped": "no-wiki-roots"}

    schema = load_wiki_schema(config.wiki_schema)
    out_dir = Path(config.export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "wiki-maintenance-worklist.json"

    extractor = list(config.wiki_graph_extractor) if config.wiki_graph_extractor else None
    load_vecs = _make_vec_loader(conn, schema)

    w = rs.run_stage1_multi(
        out_path, roots=roots, schema=schema, do_graph=True,
        load_vecs=load_vecs, graph_extractor_cmd=extractor)
    stage1_items = len(w.get("items", []))

    result = {
        "roots": [str(r) for r in roots],
        "stage1_items": stage1_items,
        "stage1_autofixes": len(w.get("auto_fixes_applied", [])),
        "adjudicated": False,
        "adjudicate_rc": None,
    }

    gateway = config.wiki_gateway
    if gateway is None:
        result["skipped_stage2"] = "no-gateway"
        return result

    default_topic = config.topics[0] if config.topics else "default"
    rc = adj.adjudicate(
        out_path, gateway=gateway, model=config.model, schema=schema, env=env,
        default_topic=default_topic, wiki_dir=roots[-1].name or "wiki",
        index_stems=_collect_index_stems(roots, schema), fallback_cwd=roots[-1].parent)
    result["adjudicated"] = True
    result["adjudicate_rc"] = rc
    return result
