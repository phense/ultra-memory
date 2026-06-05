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

from ultra_memory.maintenance._hooks import resolve_hook as _resolve_hook
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


def _make_signal_vec_loader(conn, schema: WikiSchemaConfig):
    """A load_signal_vecs(wiki_root, new_atomics) → {atomic_path: (None, vec)} over the
    ## Signal channel (Recall-Reflex): ONLY atomics that carry a ## Signal section get
    a vector. Backs the signal-axis dedup in detect_dedup (a pair with near-identical
    observables but different mechanism prose is flagged). Fail-opens to {}."""
    def load_signal_vecs(wiki_root, new_atomics):
        if not new_atomics:
            return {}
        try:
            from ultra_memory import retrieval_core, wiki_sync
            embedder = retrieval_core.default_embedder(schema.embed_model)
        except Exception:
            return {}
        items = []
        for f in _atomic_pages(Path(wiki_root), schema):
            try:
                _, _, body = split_frontmatter(f.read_text(encoding="utf-8"))
            except OSError:
                continue
            sig = wiki_sync.extract_signal_text(body)
            if sig:
                items.append(("wiki_atomic_signal", rel_atomic_path(f, wiki_root), sig))
        if not items:
            return {}
        try:
            vecs = retrieval_core.get_or_embed_batch(
                conn, items, embedder=embedder, model_name=schema.embed_model)
        except Exception:
            return {}
        return {tid: (None, vec) for tid, vec in vecs.items()}
    return load_signal_vecs


def _resolve_gateway(spec, config) -> list[str]:
    """Resolve a wiki gateway spec to an argv prefix list for the beats.

    Three spec forms are supported:

    * ``None`` / empty string → the **built-in** ``WikiGateway`` (turnkey):
      returns ``["python", "-m", "ultra_memory.wiki_gateway"]``.  The beats
      extend this prefix with the verb + args:
      ``["python", "-m", "ultra_memory.wiki_gateway", "create-page", …]``.

    * ``"module:Class"`` → a consumer subclass on the scripts path (the Phase-1C
      Trading form is ``"wiki_lib:TradingWikiGateway"``):  returns
      ``["python", "-m", "ultra_memory.wiki_gateway", "--gateway-class", spec]``.
      The base CLI's ``--gateway-class`` flag imports the class and binds it so
      the verb runs through the consumer's overrides.

    * A file-system path (no ``":"`` after a slash, or file exists) → **back-compat
      uv-run** from before Phase-1B:  returns ``["uv", "run", <path>]``.

    Fail-open: a bad spec falls back to the built-in rather than wedging the beat.
    """
    import sys as _sys

    # spec may be a Path (a resolved real-path gateway), a "module:Class" str, "" or
    # None — coerce to a stripped string before the form dispatch.
    spec = ("" if spec is None else str(spec)).strip()

    # Unset / empty → built-in turnkey.
    if not spec:
        return [_sys.executable, "-m", "ultra_memory.wiki_gateway"]

    # Path-style (back-compat): no ":" at all, or ends in ".py", or it exists as a
    # filesystem path.  Must be checked BEFORE the module:Class split so an absolute
    # path like "/some/dir/wiki_lib.py" (which contains a ":") on Windows is still
    # handled (Python on macOS/Linux paths never contain ":"); on POSIX systems an
    # absolute path starts with "/" and ":" can only appear after the leading slash in
    # drive letters (Windows), so a simple heuristic is: no ":" OR the part before ":"
    # looks like a filesystem path.
    parts = spec.split(":", 1)
    is_path = (
        len(parts) == 1                  # no colon → definitely a path
        or spec.startswith("/")          # absolute POSIX path
        or spec.startswith("./")
        or spec.startswith("../")
        or spec.endswith(".py")          # explicit script extension
        or Path(spec).exists()           # actual file on disk
    )
    if is_path:
        return ["uv", "run", spec]

    # module:Class form.
    mod_name, cls_name = parts[0], parts[1]
    if not mod_name or not cls_name:
        # Malformed → fall back to built-in, log a warning.
        print(f"[wiki_curate] _resolve_gateway: malformed spec {spec!r}, "
              "falling back to the built-in WikiGateway", file=_sys.stderr)
        return [_sys.executable, "-m", "ultra_memory.wiki_gateway"]

    return [_sys.executable, "-m", "ultra_memory.wiki_gateway", "--gateway-class", spec]


def _resolve_linter(config):
    """The Stage-1 lint hook (config.wiki_linter) → findings producer, else None."""
    return _resolve_hook(config, getattr(config, "wiki_linter", ""), "wiki_linter")


def _resolve_merge_decider(config):
    """The grey-zone merge hook (config.wiki_merge_decider) → (cosine, claim, cand)->bool,
    else None (the engine's auto-merge-only default)."""
    return _resolve_hook(config, getattr(config, "wiki_merge_decider", ""), "wiki_merge_decider")


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
    load_signal_vecs = _make_signal_vec_loader(conn, schema)
    return rs.run_stage1_multi(
        out_path, roots=roots, schema=schema, do_graph=True,
        load_vecs=load_vecs, load_signal_vecs=load_signal_vecs,
        graph_extractor_cmd=extractor, lint_findings=_resolve_linter(config))


def stage2_adjudicate(conn, config, *, worklist_path, schema=None, env=None):
    """Stage-2: run the OAuth adjudication over the worklist via the consumer gateway.
    Returns the adjudicate exit code, or None when there is no root / no gateway."""
    roots = _active_roots(config)
    if not roots or config.wiki_gateway is None:
        return None
    schema = schema or load_wiki_schema(config.wiki_schema)
    default_topic = config.topics[0] if config.topics else "default"
    # Positively log which grey-zone decider is active so the cron log surfaces a
    # silent degradation (a fail-open hook resolution that fell back to the default).
    md = _resolve_merge_decider(config)
    import sys as _sys
    print(f"[wiki_curate] stage2 merge_decider: "
          f"{'calibrated:' + config.wiki_merge_decider if md else 'engine-default (auto-merge-only)'}",
          file=_sys.stderr)
    # Resolve the gateway spec (unset/"" → built-in turnkey; "module:Class" →
    # --gateway-class; a path → uv-run) into the argv prefix the apply path shells.
    gateway_prefix = _resolve_gateway(config.wiki_gateway, config)
    return adj.adjudicate(
        worklist_path, gateway=config.wiki_gateway, gateway_prefix=gateway_prefix,
        model=config.model, schema=schema,
        env=env, default_topic=default_topic, wiki_dir=roots[-1].name or "wiki",
        index_stems=_collect_index_stems(roots, schema), fallback_cwd=roots[-1].parent,
        merge_decider=md)


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
