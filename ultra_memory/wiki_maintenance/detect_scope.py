"""detect_scope — Stage-1 scope detector (move-with-config, the 15-seam module).

Responsibilities (all read-only detection + safe non-link auto-fixes; NO LLM, NO file
rename):

  * ``new_atomics_since(ref, wiki_root, schema)`` — git log → concept pages added since
    a ref (under any topic's atomics subdir).
  * ``run(wiki_root, w, *, new_atomic_paths, write_text, today, schema)`` — single read
    of every wiki page, then three passes:
      Pass A: ``fix_missing_updated`` + anchor-collision (frontmatter-only rewrite).
      Pass B: theme-index empty-section removal + uncategorized-bullet routing.
      Pass C: missing-theme-index (C-1) + canonical-theme-index-not-linked (C-2),
              both topic-aware.

Every site-specific convention — the frontmatter field names, the atomics subdir, the
index page-types, the index-name template, the topic master filename, the auto-added
section name — is a WikiSchemaConfig seam. ``write_text(path, text)`` is injected so the
function is unit-testable without touching disk; the orchestrator binds the real,
wiki-write-locked writer.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

from ultra_memory.wiki_maintenance import autofix
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig
from ultra_memory.wiki_maintenance.wiki_util import (
    git_lines,
    rel_atomic_path,
    split_frontmatter,
    today_iso,
    wiki_md_files,
)
from ultra_memory.wiki_maintenance.worklist import (
    add_item,
    new_worklist,
    record_autofix,
    write_worklist,
    finalize,
)


# ---------------------------------------------------------------------------
# Public API: new_atomics_since
# ---------------------------------------------------------------------------

def new_atomics_since(ref: str, wiki_root, *,
                      schema: WikiSchemaConfig | None = None) -> list[str]:
    """Paths of atomics-subdir ``*.md`` files added under the wiki since *ref*.

    Uses ``git log <ref>..HEAD --name-status --diff-filter=A -- <wiki_dir>/`` and keeps
    the ``A\\t<path>`` lines whose path lives under ``schema.atomics_subdir`` (e.g.
    ``wiki/<topic>/concepts/foo.md`` OR the flat ``wiki/concepts/foo.md``). Paths are
    repo-root-relative (the ``<wiki_dir>/…`` form). Fail-open: a git error → ``[]``.
    """
    schema = schema or WikiSchemaConfig()
    wiki_root = Path(wiki_root)
    repo_root = wiki_root.parent
    wiki_dir = wiki_root.name
    seg = f"/{schema.atomics_subdir}/"
    try:
        lines = git_lines(
            "log", f"{ref}..HEAD", "--name-status", "--diff-filter=A",
            "--", f"{wiki_dir}/", repo_root=repo_root)
    except subprocess.CalledProcessError as exc:
        print(f"WARNING: new_atomics_since: git log failed ({exc}); returning []",
              file=sys.stderr)
        return []
    except Exception as exc:  # noqa: BLE001 — fail-open
        print(f"WARNING: new_atomics_since: unexpected error ({exc}); returning []",
              file=sys.stderr)
        return []

    results: list[str] = []
    for line in lines:
        if not line.startswith("A\t"):
            continue
        path = line[2:].strip()
        if (path.startswith(f"{wiki_dir}/") and path.endswith(".md")
                and seg in "/" + path):
            results.append(path)
    return results


# ---------------------------------------------------------------------------
# Frontmatter-only anchor rewrite.
# ---------------------------------------------------------------------------

def _rewrite_anchor_in_frontmatter(text: str, anchor_field: str,
                                   old_anchor: str, new_anchor: str) -> str:
    """Replace ``<anchor_field>: <old_anchor>`` in the YAML frontmatter block only,
    leaving any body occurrence untouched. Returns rewritten text (caller checks
    whether it changed)."""
    if not text.startswith("---\n"):
        return text
    after_open = 4
    rest = text[after_open:]
    idx = rest.find("\n---\n")
    if idx != -1:
        fm_raw = rest[:idx + 1]
        after_fm = rest[idx + 1:]
    elif rest.endswith("\n---"):
        fm_raw = rest[:len(rest) - 4]
        after_fm = rest[len(rest) - 4:]
    else:
        return text
    new_fm = re.sub(
        r"(?m)^" + re.escape(anchor_field) + r": " + re.escape(old_anchor) + r"[ \t]*$",
        f"{anchor_field}: {new_anchor}",
        fm_raw,
    )
    if new_fm == fm_raw:
        return text
    return "---\n" + new_fm + after_fm


# ---------------------------------------------------------------------------
# Markdown list-item detection (Pass B).
# ---------------------------------------------------------------------------

_LIST_ITEM_RE = re.compile(r"^[-*]\s+\S")
_FULL_EMPHASIS_RE = re.compile(r"^(\*[^*].*\*|_[^_].*_)$")


def _is_list_item(stripped: str) -> bool:
    """True only for a genuine Markdown bullet, not italic boilerplate prose."""
    if not _LIST_ITEM_RE.match(stripped):
        return False
    if _FULL_EMPHASIS_RE.match(stripped):
        return False
    return True


# ---------------------------------------------------------------------------
# Public API: run
# ---------------------------------------------------------------------------

def run(
    wiki_root,
    w: dict,
    *,
    new_atomic_paths: list[str],
    write_text: Callable[[Path | str, str], None],
    today: str | None = None,
    schema: WikiSchemaConfig | None = None,
) -> None:
    """Stage-1 scope detector entry point. Mutates *w* in place; calls
    *write_text(path, text)* for every file a safe auto-fix modifies."""
    schema = schema or WikiSchemaConfig()
    if today is None:
        today = today_iso()
    wiki_root = Path(wiki_root)

    type_field = schema.type_field
    title_field = schema.title_field
    theme_field = schema.theme_field
    anchor_field = schema.anchor_field
    index_types = set(schema.index_types)
    atomics_subdir = schema.atomics_subdir
    master_name = schema.topic_master_index
    # The index filename suffix, derived from the template (e.g. "-index.md").
    index_suffix = schema.index_name_template.format(slug="")
    autoadded_re = autofix.autoadded_section_re(schema)

    def _rel(p) -> str:
        return rel_atomic_path(p, wiki_root)

    w["new_atomics"] = list(new_atomic_paths)

    # Single-pass read.
    all_files: list[tuple[Path, dict, str]] = []
    for f in wiki_md_files(wiki_root):
        try:
            raw = f.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, _raw_fm, _body = split_frontmatter(raw)
        all_files.append((f, fm, raw))

    # Separate concept atomics from index files (master-over-masters, topic master,
    # theme-index in any atomics subdir).
    concepts: list[tuple[Path, dict, str]] = []
    index_files: list[tuple[Path, dict, str]] = []
    for f, fm, text in all_files:
        is_top_level_index = (f.parent == wiki_root and "index" in f.name.lower())
        is_topic_master = (
            f.parent.parent == wiki_root
            and f.name == master_name
            and fm.get(type_field) in index_types
        )
        is_concept_index = (
            f.parent.name == atomics_subdir
            and f.name.endswith(index_suffix)
            and fm.get(type_field) in index_types
        )
        if is_top_level_index or is_topic_master or is_concept_index:
            index_files.append((f, fm, text))
        else:
            concepts.append((f, fm, text))

    # ---- Pass A ----------------------------------------------------------
    current_text: dict[Path, str] = {f: text for f, _fm, text in all_files}
    current_fm: dict[Path, dict] = {f: fm for f, fm, _text in all_files}

    for f, fm, text in all_files:
        out, detail = autofix.fix_missing_updated(text, today=today, schema=schema)
        if detail is not None:
            write_text(f, out)
            current_text[f] = out
            record_autofix(w, kind=detail["kind"], path=str(f), detail=detail["detail"])

    taken_anchors: dict[str, Path] = {}
    for f in sorted(current_fm.keys()):
        fm = current_fm[f]
        anchor = fm.get(anchor_field)
        if not anchor or not isinstance(anchor, str):
            continue
        if anchor not in taken_anchors:
            taken_anchors[anchor] = f
            continue
        title = fm.get(title_field, "")
        new_anchor, coll_detail = autofix.fix_anchor_collision(
            anchor=anchor, claim=title or anchor,
            taken=set(taken_anchors.keys()), schema=schema)
        if coll_detail is None:
            continue
        text_now = current_text[f]
        rewritten = _rewrite_anchor_in_frontmatter(text_now, anchor_field, anchor, new_anchor)
        if rewritten != text_now:
            write_text(f, rewritten)
            current_text[f] = rewritten
            record_autofix(w, kind="anchor-collision", path=str(f),
                           detail=f"{anchor} -> {new_anchor}")
            taken_anchors[new_anchor] = f
        add_item(
            w, kind="recategorize", atomic_path=_rel(f),
            title=fm.get(title_field, f.stem),
            claim=f"anchor {anchor}->{new_anchor} changed; verify inbound "
                  f"[[page#{anchor}]] section refs",
            evidence=f"anchor collision: {anchor} already claimed by {taken_anchors[anchor]}",
            priority=2, kinds=schema.kinds)

    # ---- Pass B ----------------------------------------------------------
    for f, fm, _orig_text in index_files:
        text_now = current_text[f]
        out, detail = autofix.fix_empty_autoadded_section(text_now, schema=schema)
        if detail is not None:
            write_text(f, out)
            current_text[f] = out
            record_autofix(w, kind=detail["kind"], path=str(f), detail=detail["detail"])
        else:
            section_match = autoadded_re.search(text_now)
            if section_match:
                for line in section_match.group("body").splitlines():
                    stripped = line.strip()
                    if not _is_list_item(stripped):
                        continue
                    add_item(
                        w, kind="recategorize", atomic_path=_rel(f),
                        title=fm.get(title_field, f.stem), claim=stripped,
                        evidence="uncategorized index bullet", priority=2,
                        kinds=schema.kinds)

    # ---- Pass C ----------------------------------------------------------
    def _topic_of(path: Path) -> str | None:
        try:
            rel_parts = path.relative_to(wiki_root).parts
        except ValueError:
            return None
        if len(rel_parts) >= 3 and rel_parts[1] == atomics_subdir:
            return rel_parts[0]
        return None

    def _concepts_dir(topic: str | None) -> Path:
        return (wiki_root / topic / atomics_subdir) if topic else (wiki_root / atomics_subdir)

    def _topic_master_path(topic: str | None) -> Path:
        return (wiki_root / topic / master_name) if topic else (wiki_root / master_name)

    # C-1: missing theme-index (topic-aware).
    themes_by_topic: dict[str | None, set[str]] = {}
    for f, fm, _text in concepts:
        if f.parent.name != atomics_subdir:
            continue
        theme = fm.get(theme_field)
        if theme and isinstance(theme, str):
            themes_by_topic.setdefault(_topic_of(f), set()).add(theme.strip())

    for topic in sorted(themes_by_topic, key=lambda t: (t is not None, t or "")):
        for theme in sorted(themes_by_topic[topic]):
            expected = _concepts_dir(topic) / schema.index_filename(theme)
            if not expected.exists():
                add_item(
                    w, kind="index-create", atomic_path=_rel(expected),
                    title=f"Missing index for theme: {theme}",
                    claim=f"theme '{theme}' has atomics but no {schema.index_filename(theme)}",
                    theme=theme, evidence="no canonical theme-index", priority=3,
                    kinds=schema.kinds)

    # C-2: canonical theme-index not linked from its topic master.
    _master_links_cache: dict[str | None, set[str]] = {}

    def _master_links(topic: str | None) -> set[str]:
        if topic in _master_links_cache:
            return _master_links_cache[topic]
        try:
            master_text = _topic_master_path(topic).read_text(encoding="utf-8")
        except OSError:
            master_text = ""
        links = {
            link.rsplit("/", 1)[-1]
            for link in re.findall(r"\[\[([^\]|#]+?)(?:[|#][^\]]+?)?\]\]", master_text)
        }
        _master_links_cache[topic] = links
        return links

    canonical_stems_by_topic: dict[str | None, set[str]] = {
        topic: {Path(schema.index_filename(t)).stem for t in themes}
        for topic, themes in themes_by_topic.items()
    }

    for f, fm, _text in index_files:
        if fm.get(type_field, "") not in index_types:
            continue
        if f.parent.name != atomics_subdir:
            continue
        topic = _topic_of(f)
        stem = f.stem
        if stem not in canonical_stems_by_topic.get(topic, set()):
            continue
        if stem not in _master_links(topic):
            add_item(
                w, kind="recategorize", atomic_path=_rel(f),
                title=fm.get(title_field, f.stem),
                claim=f"canonical theme-index '{stem}' not linked from {master_name}",
                theme=fm.get(theme_field),
                evidence=f"canonical theme-index not linked from {master_name}",
                priority=2, kinds=schema.kinds)


# ---------------------------------------------------------------------------
# CLI entry point.
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="detect_scope (generic wiki-maintenance).")
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--since-ref", default="HEAD~1")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    w = new_worklist(str(args.wiki_root), generated_at=today_iso())
    new_paths = new_atomics_since(args.since_ref, args.wiki_root)

    def _real_write(path, text: str) -> None:
        Path(path).write_text(text, encoding="utf-8")

    run(args.wiki_root, w, new_atomic_paths=new_paths, write_text=_real_write)
    finalize(w)
    write_worklist(w, args.out)
    print(f"Worklist written to {args.out} ({len(w['items'])} items, "
          f"{len(w['auto_fixes_applied'])} autofixes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
