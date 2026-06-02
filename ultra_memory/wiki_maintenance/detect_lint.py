"""detect_lint — generic, schema-driven wiki lint + the finding router (split → core).

Self-contained: ``collect_pages`` + ``lint`` produce a findings dict over a
Karpathy-style wiki (required-frontmatter, size caps, broken [[wikilinks]], orphans,
duplicate slugs) entirely from WikiSchemaConfig; ``route_findings`` turns those findings
into worklist items / broken-link auto-fixes (the kinds are the generic worklist
taxonomy). ``build_rename_index`` reads git rename history for the conservative
single-target link repointer.

A consumer with a richer linter may produce its own findings dict (same shape) and feed
it straight to ``route_findings`` — the router does not depend on the engine's ``lint``.
NO LLM. Writes go through an injected ``write_text`` so the surface is unit-testable.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from ultra_memory.wiki_maintenance import autofix
from ultra_memory.wiki_maintenance import worklist as wl
from ultra_memory.wiki_maintenance.schema_config import WikiSchemaConfig
from ultra_memory.wiki_maintenance.wiki_util import split_frontmatter, wiki_md_files

# A [[wikilink]] target: capture the slug, drop any |alias or #anchor, strip area path.
_LINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:[|#][^\]]+?)?\]\]")


def _link_slugs(text: str) -> set[str]:
    return {m.rsplit("/", 1)[-1] for m in _LINK_RE.findall(text)}


# ---------------------------------------------------------------------------
# collect_pages.
# ---------------------------------------------------------------------------

def collect_pages(wiki_root) -> list[dict]:
    """Read every wiki page into a lint record: ``{path (wiki-relative), slug, type,
    frontmatter, body, lines, links, malformed, error}``."""
    wiki_root = Path(wiki_root)
    pages: list[dict] = []
    for f in wiki_md_files(wiki_root):
        rel = str(f.relative_to(wiki_root))
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as exc:
            pages.append({"path": rel, "slug": f.stem, "type": None, "frontmatter": {},
                          "body": "", "lines": 0, "links": set(), "malformed": False,
                          "error": str(exc)})
            continue
        fm, _raw, body = split_frontmatter(text)
        malformed = text.startswith("---\n") and not fm
        pages.append({
            "path": rel, "slug": f.stem, "type": fm.get("type"),
            "frontmatter": fm, "body": body,
            "lines": len(body.splitlines()), "links": _link_slugs(text),
            "malformed": malformed, "error": None,
        })
    return pages


# ---------------------------------------------------------------------------
# lint.
# ---------------------------------------------------------------------------

def lint(pages: list[dict], *, schema: WikiSchemaConfig | None = None) -> dict:
    """Run the generic checks over *pages*; return a findings dict (the shape
    ``route_findings`` consumes). Required-fm, size caps and the index page-types come
    from the schema."""
    schema = schema or WikiSchemaConfig()
    type_field = schema.type_field
    index_types = set(schema.index_types)
    soft, hard = schema.page_soft_cap_lines, schema.page_hard_cap_lines

    findings = {"broken_links": [], "oversized_soft": [], "oversized_hard": [],
                "missing_frontmatter": [], "malformed_frontmatter": [], "orphans": [],
                "duplicate_slugs": [], "read_errors": [], "summary": {}}

    known_slugs = {p["slug"] for p in pages if not p["error"]}
    inbound: dict[str, int] = defaultdict(int)
    slug_to_paths: dict[str, list[str]] = defaultdict(list)

    for p in pages:
        if p["error"]:
            findings["read_errors"].append({"path": p["path"], "error": p["error"]})
            continue
        slug_to_paths[p["slug"]].append(p["path"])
        for tgt in p["links"]:
            if tgt in known_slugs and tgt != p["slug"]:
                inbound[tgt] += 1

    for p in pages:
        if p["error"]:
            continue
        path = p["path"]
        if p["malformed"]:
            findings["malformed_frontmatter"].append({"path": path})
        else:
            required = schema.type_required_fm.get(p["type"], schema.base_required_fm)
            missing = [fld for fld in required if fld not in p["frontmatter"]]
            if missing:
                findings["missing_frontmatter"].append({"path": path, "missing": missing})
        if p["lines"] > hard:
            findings["oversized_hard"].append({"path": path, "lines": p["lines"]})
        elif p["lines"] > soft:
            findings["oversized_soft"].append({"path": path, "lines": p["lines"]})
        for tgt in sorted(p["links"]):
            if tgt not in known_slugs:
                findings["broken_links"].append(
                    {"from": p["slug"], "from_path": path, "to": tgt})
        # Orphan: a non-index page with no inbound links.
        if p["type"] not in index_types and inbound.get(p["slug"], 0) == 0:
            findings["orphans"].append({"path": path, "slug": p["slug"]})

    for slug, paths in sorted(slug_to_paths.items()):
        if len(paths) > 1:
            findings["duplicate_slugs"].append({"slug": slug, "paths": sorted(paths)})

    findings["summary"] = {k: len(v) for k, v in findings.items() if k != "summary"}
    findings["summary"]["total_pages"] = len(pages)
    return findings


# ---------------------------------------------------------------------------
# build_rename_index.
# ---------------------------------------------------------------------------

_RENAME_LINE_RE = re.compile(r"^R\d+\t(.+)\t(.+)$")


def build_rename_index(*, repo_root, wiki_subpath: str = "wiki",
                       runner=subprocess.run) -> dict[str, list[str]]:
    """Parse ``git log --diff-filter=R --name-status -- <wiki_subpath>/`` into
    ``{old_slug: [new_slug, ...]}``. A slug renamed to >1 distinct target keeps a
    multi-entry list (the autofix stays conservative/no-op on ambiguity). Fail-open:
    git unavailable / error → ``{}``."""
    try:
        result = runner(
            ["git", "log", "--diff-filter=R", "--name-status", "--format=",
             "--", wiki_subpath + "/"],
            cwd=str(repo_root), capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return {}
    result_map: dict[str, list[str]] = defaultdict(list)
    for line in result.stdout.splitlines():
        m = _RENAME_LINE_RE.match(line)
        if not m:
            continue
        old_slug = Path(m.group(1).strip()).stem
        new_slug = Path(m.group(2).strip()).stem
        if new_slug not in result_map[old_slug]:
            result_map[old_slug].append(new_slug)
    return dict(result_map)


# ---------------------------------------------------------------------------
# route_findings.
# ---------------------------------------------------------------------------

def _wiki_rel(path: str, wiki_dir: str) -> str:
    """Normalize a wiki-relative path to the ``<wiki_dir>/``-prefixed form the Stage-2
    apply resolver expects. Idempotent."""
    p = str(path).lstrip("/")
    if p == wiki_dir or p.startswith(wiki_dir + "/"):
        return p
    return f"{wiki_dir}/{p}"


def route_findings(findings: dict, w: dict, *,
                   schema: WikiSchemaConfig | None = None,
                   rename_index: dict[str, list[str]],
                   read_text, write_text, wiki_dir: str = "wiki") -> None:
    """Route each finding to an auto-fix or a worklist item. The broken-link autofix
    uses the raw ``from_path`` (the injected I/O resolves it); only the worklist
    ``atomic_path`` is ``<wiki_dir>/``-prefixed. ``stale_pages`` / ``suggested_pages``
    (if present) are skipped — other detectors own those."""
    schema = schema or WikiSchemaConfig()
    kinds = schema.kinds

    for finding in findings.get("broken_links", []):
        target = finding["to"]
        from_path = finding["from_path"]
        targets = rename_index.get(target, [])
        text = read_text(from_path)
        new_text, detail = autofix.fix_broken_wikilink(text, broken=target, rename_targets=targets)
        if detail is not None:
            write_text(from_path, new_text)
            wl.record_autofix(w, kind="broken-wikilink", path=from_path,
                              detail=detail.get("detail", str(detail)))
        else:
            if len(targets) == 0:
                evidence = "broken wikilink: slug unknown (0 rename targets)"
            elif len(targets) == 1:
                evidence = "broken wikilink: slug not found in file text (1 rename target)"
            else:
                evidence = f"broken wikilink: ambiguous rename ({len(targets)} targets)"
            wl.add_item(w, kind="cross-link", atomic_path=_wiki_rel(from_path, wiki_dir),
                        title=Path(from_path).stem, claim=f"broken wikilink [[{target}]]",
                        evidence=evidence, priority=2, kinds=kinds)

    for finding in findings.get("oversized_soft", []):
        wl.add_item(w, kind="summarize", atomic_path=_wiki_rel(finding["path"], wiki_dir),
                    title=Path(finding["path"]).stem,
                    claim=f"page has {finding['lines']} lines (over soft cap)",
                    priority=3, kinds=kinds)

    for finding in findings.get("oversized_hard", []):
        wl.add_item(w, kind="summarize", atomic_path=_wiki_rel(finding["path"], wiki_dir),
                    title=Path(finding["path"]).stem,
                    claim=f"page has {finding['lines']} lines (over hard cap — must split)",
                    priority=1, kinds=kinds)

    for finding in findings.get("missing_frontmatter", []):
        wl.add_item(w, kind="recategorize", atomic_path=_wiki_rel(finding["path"], wiki_dir),
                    title=Path(finding["path"]).stem,
                    claim=f"missing frontmatter fields: {', '.join(finding.get('missing', []))}",
                    priority=2, kinds=kinds)

    for finding in findings.get("malformed_frontmatter", []):
        wl.add_item(w, kind="recategorize", atomic_path=_wiki_rel(finding["path"], wiki_dir),
                    title=Path(finding["path"]).stem,
                    claim="malformed frontmatter (unparseable YAML block)",
                    priority=2, kinds=kinds)

    for finding in findings.get("orphans", []):
        wl.add_item(w, kind="cross-link", atomic_path=_wiki_rel(finding["path"], wiki_dir),
                    title=finding["slug"], claim="orphan page — no inbound wikilinks",
                    priority=3, kinds=kinds)

    for finding in findings.get("duplicate_slugs", []):
        paths = finding["paths"]
        wl.add_item(w, kind="recategorize", atomic_path=_wiki_rel(paths[0], wiki_dir),
                    title=finding["slug"],
                    claim=f"duplicate slug across {len(paths)} files: {', '.join(paths)}",
                    priority=1, kinds=kinds)

    # A consumer linter may emit `empirical_log_issues` as raw strings shaped
    # "<path>:<line>: <message>"; route each to a recategorize item (the leading
    # path segment becomes the atomic_path). Absent for the generic lint.
    for finding in findings.get("empirical_log_issues", []):
        raw_path = str(finding).split(":", 2)[0].strip() or "unknown"
        path = _wiki_rel(raw_path, wiki_dir)
        wl.add_item(w, kind="recategorize", atomic_path=path, title=Path(path).stem,
                    claim=str(finding), priority=3, kinds=kinds)

    for finding in findings.get("read_errors", []):
        wl.add_item(w, kind="recategorize", atomic_path=_wiki_rel(finding["path"], wiki_dir),
                    title=Path(finding["path"]).stem,
                    claim=f"read error: {finding.get('error', 'unknown')}",
                    evidence="read error", priority=1, kinds=kinds)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="detect_lint (generic wiki-maintenance).")
    ap.add_argument("--wiki-root", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)

    wiki_root = args.wiki_root
    if not wiki_root.exists():
        print(f"ERROR: wiki root not found: {wiki_root}", file=sys.stderr)
        return 1

    pages = collect_pages(wiki_root)
    findings = lint(pages)
    rename_index = build_rename_index(repo_root=wiki_root.parent, wiki_subpath=wiki_root.name)
    w = wl.new_worklist(str(wiki_root), generated_at="")

    def read_text(path) -> str:
        return Path(wiki_root / path).read_text(encoding="utf-8")

    def write_text(path, text: str) -> None:
        Path(wiki_root / path).write_text(text, encoding="utf-8")

    route_findings(findings, w, rename_index=rename_index, read_text=read_text,
                   write_text=write_text, wiki_dir=wiki_root.name)
    wl.finalize(w)
    wl.write_worklist(w, args.out)
    print(f"worklist: {w['counts']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
