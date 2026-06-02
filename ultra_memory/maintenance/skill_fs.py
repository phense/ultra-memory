"""SP-10 Stage 1 — the SKILL.md MATERIALIZER / GATEWAY.

The single audited write surface for generated skills (mirrors wiki_lib.py for
the wiki). It owns:
  * the FLAT layout settled by the Stage-0 research (loader is one-level only):
    a generated skill is `<repo>/.claude/skills/gen-<slug>/SKILL.md` — the `gen-`
    prefix is the directory name AND the frontmatter `name` AND the /command name;
  * frontmatter validation (open Agent-Skills standard: name<=64, description<=1024,
    name matches the parent dir);
  * atomic SKILL.md render+write (tmp + os.replace);
  * archive-never-delete: retiring a generated skill MOVES it to
    `<repo>/.claude/skills-archive/` (outside the skills scan tree) — never rm;
  * a redacted audit jsonl row per write.

It makes NO LLM call and imports no anthropic SDK (OAuth-only upheld by
construction). The wall (aggressive_wall.SkillUnit) is the DB-side provenance
gate; this module additionally enforces the STRUCTURAL invariant in code — a
write/archive target that is not a generated-skill path raises, defeating
path-traversal even if called directly.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Engine redaction chokepoint (best-effort; fail-open if unavailable).
try:  # pragma: no cover - exercised indirectly
    from ultra_memory.redact_secrets import strip_secrets as _strip_secrets
except Exception:  # pragma: no cover
    def _strip_secrets(text):  # type: ignore
        return text


GEN_PREFIX = "gen-"
MAX_NAME_LEN = 64
MAX_DESCRIPTION_LEN = 1024

# Model B (projection-coupled skill evolution) — the managed body region. The
# frontmatter trigger is FROZEN (eval-gated once); only the text BETWEEN these two
# markers is rewritten weekly by the no-LLM projection regen (zero LLM tokens). The
# markers + heading are owned here so render (at create) and the weekly splice emit a
# byte-identical region shell.
AUTO_BEGIN = "<!-- BEGIN auto-learnings -->"
AUTO_END = "<!-- END auto-learnings -->"
AUTO_HEADING = ("## Recent learnings (auto-generated — refreshed weekly from the "
                "memory store; do not edit)")
# Generated skill names: the gen- prefix + lowercase a-z/0-9 hyphen-separated
# segments (open standard: lowercase, hyphens, no leading/trailing/double hyphen).
_NAME_RE = re.compile(r"^gen-[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillWriteError(Exception):
    """A generated-skill write/archive was refused (bad target or invalid content)."""


@dataclass
class GeneratedSkill:
    """A drafted generated skill ready to materialize. `slug` IS the directory
    name and the frontmatter `name` (open standard: name == parent dir). `index_hook`
    is the source domain the skill was induced from (drives per-domain uniqueness)."""
    slug: str
    description: str
    body: str
    paths: list[str] | None = None
    index_hook: str | None = None
    source_lesson_ids: list[str] = field(default_factory=list)
    # Model B: the seeded managed-block content (the union-blend lessons markdown,
    # rendered by memory_export.render_union_blend_block). None → render no block.
    auto_learnings_block: str | None = None


# --------------------------------------------------------------------------- #
# Paths — the FLAT generated-skill layout.
# --------------------------------------------------------------------------- #

def skills_root(repo_root) -> Path:
    return Path(repo_root) / ".claude" / "skills"


def archive_root(repo_root) -> Path:
    return Path(repo_root) / ".claude" / "skills-archive"


def skill_dir(repo_root, slug: str) -> Path:
    return skills_root(repo_root) / slug


def skill_md_path(repo_root, slug: str) -> Path:
    return skill_dir(repo_root, slug) / "SKILL.md"


def is_generated_skill_path(path) -> bool:
    """Structural guard: True iff `path` is `.../.claude/skills/gen-<slug>/SKILL.md`
    — one level under a project skills root, gen-prefixed, NOT under the archive.
    A static skill (risk-manager), a two-level `_generated/<slug>/` layout, and an
    archived skill all return False."""
    p = Path(path).resolve()
    sd = p.parent
    return (
        p.name == "SKILL.md"
        and sd.name.startswith(GEN_PREFIX)
        and sd.parent.name == "skills"
        and sd.parent.parent.name == ".claude"
        and "skills-archive" not in p.parts
    )


# --------------------------------------------------------------------------- #
# Frontmatter validation + render.
# --------------------------------------------------------------------------- #

def validate_frontmatter(slug: str, description: str, paths=None) -> str | None:
    """Return an error string, or None if valid. Enforces the open Agent-Skills
    standard limits + the gen- namespace."""
    if not isinstance(slug, str) or not _NAME_RE.match(slug):
        return f"invalid name {slug!r}: must match {_NAME_RE.pattern}"
    if len(slug) > MAX_NAME_LEN:
        return f"name too long ({len(slug)}>{MAX_NAME_LEN})"
    if not isinstance(description, str) or not description.strip():
        return "description is empty"
    if len(description) > MAX_DESCRIPTION_LEN:
        return f"description too long ({len(description)}>{MAX_DESCRIPTION_LEN})"
    # The FROZEN description must never contain the managed-block markers — a marker
    # in the frontmatter would otherwise let a weekly refresh splice over it. (The
    # body-scoped splice already protects an existing file; this rejects it at the
    # source so a generated skill is never created with that hazard.)
    if AUTO_BEGIN in description or AUTO_END in description:
        return "description contains a reserved auto-learnings marker"
    if paths is not None:
        if not isinstance(paths, list) or not all(
            isinstance(x, str) and x.strip() for x in paths
        ):
            return "paths must be a list of non-empty strings"
        if any(AUTO_BEGIN in p or AUTO_END in p for p in paths):
            return "a path contains a reserved auto-learnings marker"
    return None


def render_skill_md(skill: GeneratedSkill, *, created_by: str = "background_review") -> str:
    """Render the SKILL.md text. Frontmatter carries `created_by` so the wall
    treats the skill as loop-mutable (a missing created_by parses to 'human' =
    immutable), and `paths` so the skill only auto-competes when relevant files are
    open (Stage-0 research — bounds the hijack/listing-budget surface)."""
    fm: dict = {"name": skill.slug, "description": skill.description,
                "created_by": created_by}
    if skill.paths:
        fm["paths"] = list(skill.paths)
    block = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True,
                           default_flow_style=False).rstrip("\n")
    body = skill.body.rstrip("\n") + "\n"
    if skill.auto_learnings_block is not None:
        # The procedure shell, then the managed marked region (seeded at create).
        body = body.rstrip("\n") + "\n\n" + _marked_block(skill.auto_learnings_block) + "\n"
    return f"---\n{block}\n---\n\n{body}"


# --------------------------------------------------------------------------- #
# Model B — the managed auto-learnings region (markers + splice + rewrite).
# --------------------------------------------------------------------------- #

def _marked_block(block: str) -> str:
    """The full marked region: BEGIN marker + heading + block body + END marker.
    Ends exactly at the END marker (no trailing newline) so a re-splice is
    byte-idempotent. `block` is the lessons markdown (verbatim, already redacted at
    the store write)."""
    return f"{AUTO_BEGIN}\n{AUTO_HEADING}\n\n{block.rstrip()}\n{AUTO_END}"


def _split_frontmatter(text: str):
    """Return (frontmatter_head, body). The head is the leading `---\\n…\\n---\\n`
    block (incl. its closing delimiter); the body is everything after. A document
    with no leading frontmatter returns ("", text). Splitting here is what keeps the
    marker search BODY-SCOPED — a marker string copied into the FROZEN frontmatter
    description can never be the splice target."""
    if text.startswith("---\n"):
        close = text.find("\n---\n", 3)
        if close != -1:
            cut = close + len("\n---\n")
            return text[:cut], text[cut:]
    return "", text


def splice_auto_block(text: str, block: str) -> str:
    """Replace ONLY the `<!-- BEGIN/END auto-learnings -->` region of `text`'s BODY
    with a freshly-marked `block`, leaving the frontmatter and the rest of the body
    untouched. The search is body-scoped (after the frontmatter) so a marker that
    appears inside the FROZEN frontmatter is never the target. If the body has no
    markers (a SKILL.md predating Model B, or a hand-stripped one), the marked block
    is APPENDED (self-heal). Pure string op — idempotent for an unchanged `block`."""
    region = _marked_block(block)
    head, body = _split_frontmatter(text)
    if AUTO_BEGIN in body and AUTO_END in body:
        start = body.index(AUTO_BEGIN)
        end = body.index(AUTO_END) + len(AUTO_END)
        return head + body[:start] + region + body[end:]
    return text.rstrip("\n") + "\n\n" + region + "\n"


def rewrite_auto_block(repo_root, slug: str, block: str, *, ts: str,
                       audit_dir=None) -> Path:
    """The on-disk gateway for the weekly block refresh. Structurally gated
    (`is_generated_skill_path` — never a static/human skill), atomic (tmp +
    os.replace), audited. The DB-side provenance gate (`aggressive_wall.assert_mutable`
    on a SkillUnit) is the CALLER's responsibility (mirrors `create`); this module is
    the structural + filesystem invariant. Raises SkillWriteError on a non-generated
    target or a missing file. Returns the SKILL.md path."""
    target = skill_md_path(repo_root, slug)
    if not is_generated_skill_path(target):
        raise SkillWriteError(f"refusing non-generated target: {target}")
    if not target.is_file():
        raise SkillWriteError(f"no SKILL.md to refresh at {target}")
    text = target.read_text(encoding="utf-8")
    # Fail-closed on a corrupted body: an orphan marker (BEGIN xor END) or a
    # duplicated block would make the splice append-or-mis-target. Leave such a file
    # UNTOUCHED for human repair rather than compound the corruption (the refresh
    # caller turns this into a per-skill skip).
    _head, _body = _split_frontmatter(text)
    n_begin, n_end = _body.count(AUTO_BEGIN), _body.count(AUTO_END)
    if n_begin != n_end or n_begin > 1:
        raise SkillWriteError(
            f"malformed auto-learnings markers in {target} "
            f"(BEGIN={n_begin}, END={n_end}) — refusing to refresh")
    new_text = splice_auto_block(text, block)
    if new_text == text:
        return target                       # unchanged → idempotent no-op
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, target)                 # atomic rename (not a delete)
    _emit_audit(audit_dir, ts, {"verb": "refresh-block", "slug": slug,
                                "path": str(target)})
    return target


# --------------------------------------------------------------------------- #
# Create (atomic) + archive (never delete).
# --------------------------------------------------------------------------- #

def create(skill: GeneratedSkill, *, repo_root, ts: str,
           audit_dir=None, created_by: str = "background_review") -> Path:
    """Validate + atomically write a generated skill's SKILL.md under the FLAT
    project root. Refuses (raises SkillWriteError) any non-generated target.
    Returns the SKILL.md path. Idempotent (overwrites in place)."""
    err = validate_frontmatter(skill.slug, skill.description, skill.paths)
    if err:
        raise SkillWriteError(err)
    target = skill_md_path(repo_root, skill.slug)
    if not is_generated_skill_path(target):
        raise SkillWriteError(f"refusing non-generated target: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    text = render_skill_md(skill, created_by=created_by)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)  # atomic rename (not a delete)
    _emit_audit(audit_dir, ts, {"verb": "create", "slug": skill.slug,
                                "index_hook": skill.index_hook,
                                "description": skill.description,
                                "source_lesson_ids": skill.source_lesson_ids,
                                "path": str(target)})
    return target


def archive(slug: str, *, repo_root, ts: str, audit_dir=None) -> Path | None:
    """Retire a generated skill by MOVING its directory out of the skills scan
    tree into `.claude/skills-archive/` — reversible, NEVER rm. On a name collision
    in the archive, append a `-<ts>` (then a counter) suffix. Returns the archive
    path, or **None** if the source dir is already absent (drift — the on-disk skill
    was removed out-of-band: treat as already-archived so the supersede can still
    retire the stale backing memory, never a permanent per-domain wedge). Raises only
    if `slug` is not a generated skill."""
    if not slug.startswith(GEN_PREFIX):
        raise SkillWriteError(f"refusing to archive non-generated slug: {slug!r}")
    src = skill_dir(repo_root, slug)
    if not src.is_dir():
        # Drift: backing memory said active but the dir is gone. Do NOT raise (that
        # would wedge the domain forever) — audit it and let the caller proceed.
        _emit_audit(audit_dir, ts, {"verb": "archive-skip", "slug": slug,
                                    "reason": "source dir already absent (drift)"})
        return None
    arch = archive_root(repo_root)
    arch.mkdir(parents=True, exist_ok=True)
    dest = arch / slug
    if dest.exists():
        stamp = re.sub(r"[^0-9A-Za-z]", "", ts)[:14]
        dest = arch / f"{slug}-{stamp}"
        n = 2
        while dest.exists():
            dest = arch / f"{slug}-{stamp}-{n}"
            n += 1
    shutil.move(str(src), str(dest))  # mv — reversible, never a delete
    _emit_audit(audit_dir, ts, {"verb": "archive", "slug": slug,
                                "path": str(dest)})
    return dest


# --------------------------------------------------------------------------- #
# Audit (redacted, fail-open).
# --------------------------------------------------------------------------- #

def _audit_path(audit_dir, ts: str) -> Path:
    return Path(audit_dir) / f"sp10-writes-{str(ts)[:10]}.jsonl"


def _emit_audit(audit_dir, ts: str, row: dict) -> None:
    if not audit_dir:
        return
    try:
        red = {k: (_strip_secrets(v) if isinstance(v, str) else
                   [_strip_secrets(x) for x in v] if isinstance(v, list) else v)
               for k, v in row.items()}
        red["ts"] = ts
        path = _audit_path(audit_dir, ts)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(red, ensure_ascii=False) + "\n")
    except Exception:  # fail-open: an audit failure never blocks the write
        pass
