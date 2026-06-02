"""The Stage-1 worklist schema + read/write/aggregate helpers (move-generic).

The structural JSON record the detectors append findings to and the Stage-2
adjudicator drains. Ported verbatim from the reference pipeline except the `KINDS`
taxonomy, which is now configurable (a consumer's `WikiSchemaConfig.kinds`); a caller
with the default schema passes nothing. Pure JSON + dedup/count logic — no LLM, no
consumer import.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from ultra_memory.wiki_maintenance.schema_config import KINDS_DEFAULT

SCHEMA_VERSION = 1


def new_worklist(wiki_root: str, *, generated_at: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "wiki_root": wiki_root,
        "new_atomics": [],
        "items": [],
        "graph_findings": {"contradiction_edges": [], "high_mention_orphans": [],
                           "same_source_clusters": []},
        "auto_fixes_applied": [],
        "counts": {},
    }


def add_item(w: dict, *, kind: str, atomic_path: str, title: str, claim: str,
             source: str = "wiki", section_anchor: str | None = None,
             theme: str | None = None, candidate_path: str | None = None,
             candidate_text: str | None = None, cosine: float | None = None,
             evidence: str = "", priority: int = 3, root: str | None = None,
             kinds=None) -> None:
    """Append a finding. `kinds` (the allowed taxonomy) defaults to the reference
    KINDS_DEFAULT; a consumer with a custom schema passes `schema.kinds`. Each item
    carries its owning wiki `root` (multi-root composer keeps two same-basename
    findings distinct; single-root callers leave root=None → the pre-multi-root
    (kind, atomic_path) dedup behavior)."""
    allowed = set(kinds) if kinds is not None else set(KINDS_DEFAULT)
    if kind not in allowed:
        raise ValueError(f"unknown worklist kind: {kind}")
    w["items"].append({
        "source": source, "kind": kind, "atomic_path": atomic_path,
        "section_anchor": section_anchor, "theme": theme, "title": title,
        "claim": claim, "candidate_path": candidate_path,
        "candidate_text": candidate_text, "cosine": cosine, "evidence": evidence,
        "priority": priority, "root": root,
    })


def record_autofix(w: dict, *, kind: str, path: str, detail: str) -> None:
    w["auto_fixes_applied"].append({"kind": kind, "path": path, "detail": detail})


def is_empty(w: dict) -> bool:
    gf = w["graph_findings"]
    return (not w["items"] and not w["auto_fixes_applied"]
            and not any(gf.values()))


def finalize(w: dict) -> dict:
    """De-duplicate items by (root, kind, atomic_path) — first occurrence wins, order
    otherwise preserved — then compute counts. A same-basename page in two roots keeps
    both findings (the root component prevents a cross-root collapse)."""
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for i in w["items"]:
        key = (i.get("root"), i.get("kind"), i.get("atomic_path"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(i)
    w["items"] = deduped

    by_kind = Counter(i["kind"] for i in w["items"])
    w["counts"] = {
        "new_atomics": len(w["new_atomics"]),
        "items": len(w["items"]),
        "auto_fixes": len(w["auto_fixes_applied"]),
        "graph_findings": {k: len(v) for k, v in w["graph_findings"].items()},
        "by_kind": dict(by_kind),
    }
    return w


def write_worklist(w: dict, path) -> None:
    Path(path).write_text(json.dumps(w, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")


def read_worklist(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
