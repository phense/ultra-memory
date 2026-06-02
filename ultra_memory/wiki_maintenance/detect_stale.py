"""detect_stale — stale / oversized / unresolved-contradiction detector (move-generic).

Pure read-only scan over `wiki_md_files`, adding worklist items for:
  (a) frontmatter `<status_field>: <stale_status_marker>` → stale-archive (priority 2)
  (b) body line-count > `page_soft_cap_lines` → summarize (priority 3)
  (c) a `conflict_section_headings` block with no `resolution_marker_regex` in the
      body → contradiction (priority 2)

Every threshold/marker/field comes from WikiSchemaConfig; the algorithm is generic.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from ultra_memory.wiki_maintenance import worklist as wl
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig
from ultra_memory.wiki_maintenance.wiki_util import (
    rel_atomic_path,
    split_frontmatter,
    today_iso,
    wiki_md_files,
)


def _conflict_heading_re(headings) -> re.Pattern:
    """Build a heading regex from the schema's conflict headings, treating `-`/space
    runs as interchangeable (so 'Conflicts-with' also matches 'Conflicts With')."""
    alts = []
    for h in headings:
        parts = re.split(r"[-\s]+", str(h).strip())
        alts.append(r"[-\s]+".join(re.escape(p) for p in parts if p))
    pattern = "|".join(a for a in alts if a) or r"(?!x)x"   # never-match if empty
    return re.compile(rf"^#{{1,6}}\s+({pattern})\b", re.IGNORECASE | re.MULTILINE)


def run(wiki_root, w: dict, *, schema: WikiSchemaConfig | None = None) -> None:
    """Scan `wiki_root` and populate `w`. Pure read — no writes to any wiki file."""
    schema = schema or WikiSchemaConfig()
    conflict_re = _conflict_heading_re(schema.conflict_section_headings)
    try:
        resolved_re = re.compile(schema.resolution_marker_regex, re.IGNORECASE)
    except re.error:
        resolved_re = re.compile(r"(?!x)x")               # bad config → never resolved
    soft_cap = schema.page_soft_cap_lines

    for md_file in wiki_md_files(wiki_root):
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, _raw, body = split_frontmatter(text)
        title = fm.get(schema.title_field, md_file.stem)
        path = rel_atomic_path(md_file, wiki_root)

        if fm.get(schema.status_field) == schema.stale_status_marker:
            wl.add_item(w, kind="stale-archive", atomic_path=path, title=title,
                        claim=f"Page has {schema.status_field}: {schema.stale_status_marker} "
                              "and may need archiving or redirect.",
                        evidence=f"{schema.status_field}: {schema.stale_status_marker}",
                        priority=2, kinds=schema.kinds)

        body_lines = body.splitlines()
        if len(body_lines) > soft_cap:
            wl.add_item(w, kind="summarize", atomic_path=path, title=title,
                        claim=f"Page body is {len(body_lines)} lines, exceeding the "
                              f"{soft_cap}-line soft cap — consider splitting or summarizing.",
                        evidence=f"body_lines={len(body_lines)} > soft_cap={soft_cap}",
                        priority=3, kinds=schema.kinds)

        if conflict_re.search(body) and not resolved_re.search(body):
            wl.add_item(w, kind="contradiction", atomic_path=path, title=title,
                        claim="Page contains a conflict/variant block with no resolution marker.",
                        evidence="unresolved conflict/variant block",
                        priority=2, kinds=schema.kinds)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="detect_stale (generic wiki-maintenance).")
    ap.add_argument("--wiki-root", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    root = Path(args.wiki_root)
    w = wl.new_worklist(str(root), generated_at=today_iso())
    run(root, w)
    wl.finalize(w)
    wl.write_worklist(w, Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
