# ultra_memory/maintenance/_hooks.py
"""Shared, project-agnostic consumer-hook resolver (``module:function`` -> callable).

Factored out of wiki_curate so notify.py and wiki_curate share ONE resolver without
coupling. Inserts ``<project_dir>/scripts`` and ``<project_dir>`` onto ``sys.path`` so
an in-tree consumer module is importable. Empty / ``":"``-less / unresolvable -> None
(FAIL-OPEN: a bad hook logs one line and degrades to the engine default, never wedges
maintenance)."""
from __future__ import annotations


def resolve_hook(config, spec: str, what: str):
    import importlib
    import sys

    spec = (spec or "").strip()
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
    except Exception as exc:  # noqa: BLE001
        print(f"[hooks] could not resolve {what} {spec!r}: {exc!r} — using the default",
              file=sys.stderr)
        return None
